"""
app.py
Main Flask application.

Endpoints:
  GET  /webhook          – Facebook webhook verification
  POST /webhook          – Incoming Messenger messages
  POST /api/send-daily   – Trigger daily question (called by cron-job.org)
  GET  /api/status       – Health check
"""

import os
import random
import threading

from flask import Flask, request, jsonify, abort


import database as db
import messenger as msg
import llm
from questions import (
    load_questions,
    get_question,
    format_question_message,
    format_result_message,
)

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "aws-quiz-verify-token")
CRON_SECRET  = os.environ.get("CRON_SECRET", "")     # optional guard for /api/send-daily


# ── Startup ───────────────────────────────────────────────────────────────────

def startup():
    db.init_db()
    # Pre-load questions in a background thread so first request isn't slow
    threading.Thread(target=load_questions, daemon=True).start()


# ── Core logic ────────────────────────────────────────────────────────────────

def _pick_next_question(psid: str):
    """Pick a question the user hasn't answered yet (random order)."""
    questions = load_questions()
    answered = db.get_answered_questions(psid)
    remaining = [q for q in questions if q.number not in answered]
    if not remaining:
        return None  # All done!
    return random.choice(remaining)


def _send_daily_question_to_all_users():
    """
    Send the daily question to every known user.
    In practice you only have one PSID stored, but this scales.
    """
    import sqlite3, os

    # Gather all PSIDs from the DB
    psids = []
    try:
        if db.DATABASE_URL:
            import psycopg2
            conn = psycopg2.connect(db.DATABASE_URL, sslmode="require")
            cur = conn.cursor()
            cur.execute("SELECT psid FROM user_state")
            psids = [row[0] for row in cur.fetchall()]
            conn.close()
        else:
            conn = sqlite3.connect(db.DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT psid FROM user_state")
            psids = [row[0] for row in cur.fetchall()]
            conn.close()
    except Exception as e:
        print(f"[scheduler] DB error: {e}")
        return

    for psid in psids:
        try:
            _send_daily_question(psid)
        except Exception as e:
            print(f"[scheduler] Error sending to {psid}: {e}")


def _send_daily_question(psid: str):
    """Send a new question to one user."""
    q = _pick_next_question(psid)
    if q is None:
        msg.send_message(
            psid,
            "🎉 You've answered all available questions! "
            "Your streak is complete. Type 'stats' to see your score, "
            "or 'reset' to start over.",
        )
        return

    db.upsert_user_state(psid, current_q=q.number, state="awaiting_answer")

    question_msg = format_question_message(q)
    if q.options:
        # Send with quick-reply buttons
        letters = sorted(q.options.keys())
        msg.send_quick_replies(psid, question_msg, letters)
    else:
        msg.send_message(psid, question_msg)


def _handle_answer(psid: str, user_text: str, current_q_num: int):
    """Process the user's answer to the current question."""
    q = get_question(current_q_num)
    if q is None:
        msg.send_message(psid, "⚠️ Couldn't load that question. Type 'next' to get a new one.")
        db.upsert_user_state(psid, state="idle")
        return

    answer_letter = user_text.strip().upper()
    if len(answer_letter) != 1 or answer_letter not in "ABCDE":
        # Not a valid answer letter – treat as follow-up question
        _handle_followup(psid, user_text, q)
        return

    is_correct = answer_letter == q.correct_answer.upper() if q.correct_answer else None
    db.record_answer(psid, q.number, answer_letter, bool(is_correct))
    db.upsert_user_state(psid, current_q=q.number, state="answered")

    result_msg = format_result_message(q, answer_letter)
    msg.send_message(psid, result_msg)


def _handle_followup(psid: str, user_text: str, q=None):
    """Answer a follow-up question using the LLM."""
    msg.send_typing_on(psid)

    if q:
        answer = llm.answer_followup(
            question_text=q.text,
            correct_answer=q.correct_answer,
            explanation=q.explanation,
            user_followup=user_text,
        )
    else:
        answer = llm.answer_general(user_text)

    msg.send_message(psid, answer)


def _handle_message(psid: str, message_text: str):
    """Route incoming Messenger messages."""
    text = message_text.strip()
    text_lower = text.lower()

    # Commands
    if text_lower in ("stats", "score", "how am i doing"):
        stats = db.get_stats(psid)
        msg.send_message(
            psid,
            f"📊 Your stats:\n"
            f"  Questions answered: {stats['total']}\n"
            f"  Correct: {stats['correct']}\n"
            f"  Score: {stats['percentage']}%\n\n"
            f"Type 'next' to get a new question anytime!",
        )
        return

    if text_lower in ("reset", "start over", "restart"):
        db.upsert_user_state(psid, current_q=None, state="idle")
        msg.send_message(
            psid,
            "🔄 State reset. Your history is preserved but you can now get new questions. "
            "Type 'next' or wait for tomorrow's daily question!",
        )
        return

    if text_lower in ("next", "new question", "another", "skip"):
        _send_daily_question(psid)
        return

    if text_lower in ("help", "start", "hi", "hello"):
        msg.send_message(
            psid,
            "👋 Welcome to AWS SAA-C03 Daily Quiz Bot!\n\n"
            "📅 You'll receive one question each day at 9am AWST.\n"
            "💬 Reply with A / B / C / D to answer.\n"
            "🤔 Ask any follow-up question and I'll explain using AI.\n\n"
            "Commands:\n"
            "  next  – Get a question now\n"
            "  stats – See your score\n"
            "  reset – Reset current question\n"
            "  help  – Show this message",
        )
        # Register the user so they get daily questions
        db.upsert_user_state(psid, state="idle")
        return

    # Load user state
    state = db.get_user_state(psid)
    current_q_num = state.get("current_q")
    user_state = state.get("state", "idle")

    if user_state == "awaiting_answer" and current_q_num:
        _handle_answer(psid, text, current_q_num)
    elif user_state == "answered" and current_q_num:
        # Follow-up question after answering
        q = get_question(current_q_num)
        _handle_followup(psid, text, q)
    elif current_q_num:
        # Has a question loaded, treat as follow-up
        q = get_question(current_q_num)
        _handle_followup(psid, text, q)
    else:
        # No active question – general AWS question
        _handle_followup(psid, text, None)


# ── Flask Routes ──────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Facebook webhook verification."""
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("[webhook] Verified!")
        return challenge, 200
    abort(403)


@app.route("/webhook", methods=["POST"])
def receive_message():
    """Handle incoming Messenger events."""
    data = request.get_json(force=True)

    if data.get("object") != "page":
        return "ok", 200

    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):
            psid = event.get("sender", {}).get("id")
            if not psid:
                continue

            if "message" in event:
                message = event["message"]
                # Ignore echoes (messages sent by the bot itself)
                if message.get("is_echo"):
                    continue
                text = message.get("text", "").strip()
                if text:
                    try:
                        _handle_message(psid, text)
                    except Exception as e:
                        print(f"[webhook] Error handling message from {psid}: {e}")
                        msg.send_message(psid, "⚠️ Something went wrong. Please try again.")

            elif "postback" in event:
                # Quick-reply postback
                payload = event["postback"].get("payload", "")
                if payload:
                    try:
                        _handle_message(psid, payload)
                    except Exception as e:
                        print(f"[webhook] Error handling postback from {psid}: {e}")

    return "ok", 200


@app.route("/api/send-daily", methods=["GET", "POST"])
def trigger_daily():
    """
    Called by Vercel built-in cron (GET) or external cron like cron-job.org (POST).
    Optionally protected by a secret header (bypassed for Vercel's own cron).
    """
    is_vercel_cron = request.headers.get("x-vercel-cron") == "1"
    if CRON_SECRET and not is_vercel_cron:
        auth = request.headers.get("X-Cron-Secret", "")
        if auth != CRON_SECRET:
            abort(401)

    _send_daily_question_to_all_users()
    return jsonify({"status": "ok", "message": "Daily questions sent."})


@app.route("/api/status", methods=["GET"])
def status():
    questions = load_questions()
    return jsonify({
        "status": "running",
        "questions_loaded": len(questions),
        "llm_model": llm.MODEL,
    })


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    startup()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
else:
    # When run via gunicorn
    startup()
