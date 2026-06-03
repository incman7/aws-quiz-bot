"""
database.py
Handles all persistence: user state (current question, waiting for answer)
and quiz history (score tracking).

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

def init_db():
    """Create tables if they don't exist."""
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
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS quiz_history (
                id            SERIAL PRIMARY KEY,
                psid          TEXT,
                question_num  INTEGER,
                user_answer   TEXT,
                correct       BOOLEAN,
                answered_at   TEXT
            )
        """ if DATABASE_URL else f"""
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
            f"SELECT * FROM user_state WHERE psid = {PLACEHOLDER}",
            (psid,),
        )
        row = cur.fetchone()
    if row is None:
        return {"psid": psid, "current_q": None, "state": "idle", "last_active": None}
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
                # Full upsert — update all fields
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
                # Registration only — insert if not exists, just update last_active if exists
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
    return {"total": total, "correct": correct, "percentage": pct}


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
