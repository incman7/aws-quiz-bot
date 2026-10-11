"""
database.py
Handles all persistence: user state (current question, waiting for answer),
streak info, and quiz history (score tracking).

Uses PostgreSQL if DATABASE_URL env var is set, otherwise SQLite.
"""

import os
import json
from datetime import datetime, date
from contextlib import contextmanager

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# ── Determine driver ──────────────────────────────────────────────────────────
if DATABASE_URL:
    import psycopg2
    import psycopg2.extras

    @contextmanager
    def _get_conn():
        conn = psycopg2.connect(DATABASE_URL, sslmode="require")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    PLACEHOLDER = "%s"
else:
    import sqlite3

    DB_PATH = os.path.join(os.path.dirname(__file__), "quiz.db")

    @contextmanager
    def _get_conn():
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    PLACEHOLDER = "?"


# ── Schema setup ──────────────────────────────────────────────────────────────

def _add_column_if_missing(cur, table: str, column: str, definition: str):
    """
    Add a column to an existing table, ignoring 'column already exists' errors.
    Used so init_db() can evolve the schema without manual migrations.
    """
    if DATABASE_URL:
        # Postgres supports IF NOT EXISTS for ADD COLUMN (9.6+)
        cur.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}')
    else:
        # SQLite: probe information_schema, fall back to PRAGMA if unavailable
        try:
            cur.execute(
                f"SELECT 1 FROM pragma_table_info('{table}') WHERE name = ?",
                (column,),
            )
            if cur.fetchone() is None:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        except Exception:
            # Older SQLite — try-and-swallow
            try:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            except Exception:
                pass


def init_db():
    """Create tables if they don't exist and add any missing columns."""
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS user_state (
                psid          TEXT PRIMARY KEY,
                current_q     INTEGER,
                state         TEXT DEFAULT 'idle',
                last_active   TEXT
            )
        """)
        # Idempotency: track which day we last sent a question to each user
        _add_column_if_missing(cur, "user_state", "last_sent_date", "TEXT")
        # Streak: consecutive days the user answered
        _add_column_if_missing(cur, "user_state", "streak_count", "INTEGER DEFAULT 0")
        _add_column_if_missing(cur, "user_state", "last_answered_date", "TEXT")

        # Per-user persistent LLM conversation history so follow-ups survive
        # serverless cold starts.
        cur.execute("""
                CREATE TABLE IF NOT EXISTS llm_history (
                    psid        TEXT PRIMARY KEY,
                    history     TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                )
            """)

        if DATABASE_URL:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS quiz_history (
                    id            SERIAL PRIMARY KEY,
                    psid          TEXT,
                    question_num  INTEGER,
                    user_answer   TEXT,
                    correct       BOOLEAN,
                    answered_at   TEXT
                )
            """)
        else:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS quiz_history (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    psid          TEXT,
                    question_num  INTEGER,
                    user_answer   TEXT,
                    correct       INTEGER,
                    answered_at   TEXT
                )
            """)
    print("[db] Tables ready.")


# ── User state ────────────────────────────────────────────────────────────────

def get_user_state(psid: str) -> dict:
    """Return the user's current state row, or defaults."""
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT psid, current_q, state, last_active, last_sent_date, "
            f"streak_count, last_answered_date FROM user_state WHERE psid = {PLACEHOLDER}",
            (psid,),
        )
        row = cur.fetchone()
        if row is None:
            return {
                "psid": psid,
                "current_q": None,
                "state": "idle",
                "last_active": None,
                "last_sent_date": None,
                "streak_count": 0,
                "last_answered_date": None,
            }
        if DATABASE_URL:
            cols = [desc[0] for desc in cur.description]
            return dict(zip(cols, row))
        else:
            return dict(row)


def upsert_user_state(psid: str, current_q: int = None, state: str = None):
    """
    Insert or update user state.
    If called with only psid (registration), existing current_q/state are preserved.
    If called with explicit state/current_q, those values are updated.
    """
    now = datetime.utcnow().isoformat()
    with _get_conn() as conn:
        cur = conn.cursor()
        if DATABASE_URL:
            if state is not None:
                cur.execute(
                    f"""
                    INSERT INTO user_state (psid, current_q, state, last_active)
                    VALUES ({PLACEHOLDER}, {PLACEHOLDER}, {PLACEHOLDER}, {PLACEHOLDER})
                    ON CONFLICT (psid) DO UPDATE SET
                        current_q = EXCLUDED.current_q,
                        state = EXCLUDED.state,
                        last_active = EXCLUDED.last_active
                    """,
                    (psid, current_q, state, now),
                )
            else:
                cur.execute(
                    f"""
                    INSERT INTO user_state (psid, current_q, state, last_active)
                    VALUES ({PLACEHOLDER}, NULL, 'idle', {PLACEHOLDER})
                    ON CONFLICT (psid) DO UPDATE SET
                        last_active = EXCLUDED.last_active
                    """,
                    (psid, now),
                )
        else:
            if state is not None:
                cur.execute(
                    f"""
                    INSERT OR REPLACE INTO user_state (psid, current_q, state, last_active)
                    VALUES ({PLACEHOLDER}, {PLACEHOLDER}, {PLACEHOLDER}, {PLACEHOLDER})
                    """,
                    (psid, current_q, state, now),
                )
            else:
                cur.execute(
                    f"""
                    INSERT OR IGNORE INTO user_state (psid, current_q, state, last_active)
                    VALUES ({PLACEHOLDER}, NULL, 'idle', {PLACEHOLDER})
                    """,
                    (psid, now),
                )


def mark_question_sent(psid: str, sent_date: str):
    """Record that we sent a daily question to this user on `sent_date` (YYYY-MM-DD)."""
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE user_state SET last_sent_date = {PLACEHOLDER} WHERE psid = {PLACEHOLDER}",
            (sent_date, psid),
        )


def update_streak_on_answer(psid: str, answered_date: str) -> int:
    """
    Increment streak if the user answered yesterday; set to 1 if they skipped
    a day or this is their first answer. Returns the new streak count.
    """
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT streak_count, last_answered_date FROM user_state WHERE psid = {PLACEHOLDER}",
            (psid,),
        )
        row = cur.fetchone()
        prev_streak = 0
        prev_date = None
        if row is not None:
            if DATABASE_URL:
                prev_streak = row[0] or 0
                prev_date = row[1]
            else:
                prev_streak = (row["streak_count"] or 0) if "streak_count" in row.keys() else 0
                prev_date = row["last_answered_date"] if "last_answered_date" in row.keys() else None

        if prev_date == answered_date:
            # Already counted today's answer — leave streak as-is
            new_streak = prev_streak or 1
        else:
            try:
                prev_d = date.fromisoformat(prev_date) if prev_date else None
                curr_d = date.fromisoformat(answered_date)
            except ValueError:
                prev_d = None
                curr_d = date.fromisoformat(answered_date)

            if prev_d is not None and (curr_d - prev_d).days == 1:
                new_streak = (prev_streak or 0) + 1
            else:
                # First answer ever, or gap of more than one day
                new_streak = 1

        cur.execute(
            f"UPDATE user_state SET streak_count = {PLACEHOLDER}, last_answered_date = {PLACEHOLDER} "
            f"WHERE psid = {PLACEHOLDER}",
            (new_streak, answered_date, psid),
        )
        return new_streak


# ── Quiz history ──────────────────────────────────────────────────────────────

def record_answer(psid: str, question_num: int, user_answer: str, correct: bool):
    now = datetime.utcnow().isoformat()
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            INSERT INTO quiz_history (psid, question_num, user_answer, correct, answered_at)
            VALUES ({PLACEHOLDER}, {PLACEHOLDER}, {PLACEHOLDER}, {PLACEHOLDER}, {PLACEHOLDER})
            """,
            (psid, question_num, user_answer.upper(), correct, now),
        )


def get_answered_questions(psid: str) -> set[int]:
    """Return set of question numbers already answered by this user."""
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT DISTINCT question_num FROM quiz_history WHERE psid = {PLACEHOLDER}",
            (psid,),
        )
        rows = cur.fetchall()
    return {row[0] for row in rows}


def get_stats(psid: str) -> dict:
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN correct THEN 1 ELSE 0 END) as correct_count
            FROM quiz_history
            WHERE psid = {PLACEHOLDER}
            """,
            (psid,),
        )
        row = cur.fetchone()
    total = row[0] or 0
    correct = row[1] or 0
    pct = round(correct / total * 100) if total > 0 else 0
    state = get_user_state(psid)
    return {
        "total": total,
        "correct": correct,
        "percentage": pct,
        "streak": state.get("streak_count") or 0,
    }


def has_answered_today(psid: str) -> bool:
    today = date.today().isoformat()
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT 1 FROM quiz_history
            WHERE psid = {PLACEHOLDER} AND answered_at LIKE {PLACEHOLDER}
            LIMIT 1
            """,
            (psid, f"{today}%"),
        )
        return cur.fetchone() is not None


def get_users_due_for_question(today_iso: str) -> list[str]:
    """
    Return PSIDs of every registered user who has not been sent a question
    on `today_iso` yet. Used by the cron to make the daily send a no-op on
    duplicate firings.
    """
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT psid FROM user_state WHERE last_sent_date IS NULL OR last_sent_date != {PLACEHOLDER}",
            (today_iso,),
        )
        return [row[0] for row in cur.fetchall()]


# ── Persistent LLM history ────────────────────────────────────────────────────

def load_llm_history(psid: str) -> list:
    """Return this user's LLM conversation history (empty list if none)."""
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT history FROM llm_history WHERE psid = {PLACEHOLDER}",
            (psid,),
        )
        row = cur.fetchone()
    if row is None:
        return []
    raw = row[0]
    try:
        return json.loads(raw)
    except Exception:
        return []


def save_llm_history(psid: str, history: list):
    """Persist the user's LLM conversation history (JSON-encoded)."""
    if not history:
        return
    now = datetime.utcnow().isoformat()
    payload = json.dumps(history, ensure_ascii=False)
    with _get_conn() as conn:
        cur = conn.cursor()
        if DATABASE_URL:
            cur.execute(
                f"""
                INSERT INTO llm_history (psid, history, updated_at)
                VALUES ({PLACEHOLDER}, {PLACEHOLDER}, {PLACEHOLDER})
                ON CONFLICT (psid) DO UPDATE SET
                    history = EXCLUDED.history,
                    updated_at = EXCLUDED.updated_at
                """,
                (psid, payload, now),
            )
        else:
            cur.execute(
                f"""
                INSERT OR REPLACE INTO llm_history (psid, history, updated_at)
                VALUES ({PLACEHOLDER}, {PLACEHOLDER}, {PLACEHOLDER})
                """,
                (psid, payload, now),
            )


def clear_llm_history(psid: str):
    """Drop any persisted LLM history for this user."""
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"DELETE FROM llm_history WHERE psid = {PLACEHOLDER}",
            (psid,),
        )