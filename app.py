"""
app.py
Main Flask application.

Endpoints:
  GET  /webhook          – Facebook webhook verification
  POST /webhook          – Incoming Messenger messages
  GET/POST /api/send-daily – Trigger daily question (called by cron)
  GET  /api/status       – Health check
"""

import logging
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

# ── Logging setup ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("app")

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "aws-quiz-verify-token")
CRON_SECRET  = os.environ.get("CRON_SECRET", "")

# Log env var presence at startup (never log values)
log.info("Startup — VERIFY_TOKEN set: %s", bool(VERIFY_TOKEN))
log.info("Startup — CRON_SECRET set: %s", bool(CRON_SECRET))
log.info("Startup — DATABASE_URL set: %s", bool(db.DATABASE_URL))
log.info("Startup — PAGE_ACCESS_TOKEN set: %s", bool(os.environ.get("PAGE_ACCESS_TOKEN")))
log.info("Startup — GROQ_API_KEY set: %s", bool(os.environ.get("GROQ_API_KEY")))


# ── Startup ───────────────────────────────────────────────────────────────────

def startup():
    log.info("Initialising database...")
    db.init_db()
    log.info("Database initialised.")
    threading.Thread(target=load_questions, daemon=True).start()


# ── Core logic ────────────────────────────────────────────────────────────────

def _pick_next_question(psid: str):
    """Pick a question the user hasn't answered yet (random order)."""
    questions = load_questions()
    answered = db.get_answered_questions(psid)
    # Only include well-formed questions (at least 2 options)
    remaining = [q for q in questions if q.number not in answered and len(q.options) >= 2]
    log.info("[pick_question] psid=%s total=%d answered=%d remaining=%d",
             psid, len(questions), len(answered), len(remaining))
    if not remaining:
        return None
    return random.choice(remaining)


def _send_daily_question_to_all_users():
    """Send the daily question to every known user. Returns a result dict."""
    log.info("[cron] Starting daily send. DATABASE_URL set: %s", bool(db.DATABASE_URL))

    psids = []
    try:
        with db._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT psid FROM user_state")
            psids = [row[0] for row in cur.fetchall()]
        log.info("[cron] Found %d user(s) in user_state: %s", len(psids), psids)
    except Exception as e:
        log.error("[cron] DB error fetching users: %s", e, exc_info=True)
        return {"psids_found": 0, "db_error": str(e)}

    if not psids:
        log.warning("[cron] No users found — nobody will receive a question. "
                    "Make sure at least one person has sent 'hi' to the bot.")

    errors = []
    for psid in psids:
        try:
            log.info("[cron] Sending question to psid=%s", psid)
            _send_daily_question(psid)
            log.info("[cron] Successfully sent to psid=%s", psid)
        except Exception as e:
            log.error("[cron] Failed to send to psid=%s: %s", psid, e, exc_info=True)
            errors.append({"psid": psid, "error": str(e)})

    log.info("[cron] Done. sent=%d errors=%d", len(psids) - len(errors), len(errors))
    return {"psids_found": len(psids), "sent": len(psids) - len(errors), "errors": errors}


def _send_daily_question(psid: str):
    """Send a new question to one user."""
    q = _pick_next_question(psid)
    if q is None:
        log.info("[send_question] psid=%s has answered all questions", psid)
        msg.send_message(
            psid,
            "🎉 You've answered all available questions! "
            "Your streak is complete. Type 'stats' to see your score, "
            "or 'reset' to start over.",
        )
        return

    log.info("[send_question] psid=%s sending question=%d", psid, q.number)
    llm.clear_history(psid)  # Clear any previous question's conversation
    db.upsert_user_state(psid, current_q=q.number, state="awaiting_answer")

    question_msg = format_question_message(q)
    if q.options:
        letters = sorted(q.options.keys())
        msg.send_quick_replies(psid, question_msg, letters)
    else:
        msg.send_message(psid, question_msg)


def _handle_answer(psid: str, user_text: str, current_q_num: int):
    """Process the user's answer to the current question."""
    log.info("[answer] psid=%s q=%d answer=%s", psid, current_q_num, user_text.strip().upper())
    q = get_question(current_q_num)
    if q is None:
        log.warning("[answer] Question %d not found for psid=%s", current_q_num, psid)
        msg.send_message(psid, "⚠️ Couldn't load that question. Type 'next' to get a new one.")
        db.upsert_user_state(psid, state="idle")
        return

    answer_letter = user_text.strip().upper()
    if len(answer_letter) != 1 or answer_letter not in "ABCDE":
        log.info("[answer] psid=%s input '%s' not a valid answer — treating as follow-up", psid, user_text)
        _handle_followup(psid, user_text, q)
        return

    is_correct = answer_letter == q.correct_answer.upper() if q.correct_answer else None
    log.info("[answer] psid=%s answered %s, correct=%s", psid, answer_letter, is_correct)
    db.record_answer(psid, q.number, answer_letter, bool(is_correct))
    db.upsert_user_state(psid, current_q=q.number, state="answered")

    result_msg = format_result_message(q, answer_letter)
    msg.send_message(psid, result_msg)

    # Seed LLM conversation history so follow-ups have full question context
    llm.start_question_context(
        psid=psid,
        question_text=q.text,
        correct_answer=q.correct_answer or "",
        explanation=q.explanation or "",
    )


def _handle_followup(psid: str, user_text: str, q=None):
    """Answer a follow-up question using the LLM."""
    log.info("[followup] psid=%s q=%s text='%s'", psid, q.number if q else None, user_text[:80])
    msg.send_typing_on(psid)

    if q:
        answer = llm.answer_followup(psid=psid, user_followup=user_text)
    else:
        answer = llm.answer_general(psid=psid, user_message=user_text)

    log.info("[followup] psid=%s LLM response length=%d", psid, len(answer))
    msg.send_message(psid, answer)


def _handle_message(psid: str, message_text: str):
    """Route incoming Messenger messages."""
    text = message_text.strip()
    text_lower = text.lower()
    log.info("[message] psid=%s text='%s'", psid, text[:80])

    # Always ensure user is registered so the cron job can find them
    db.upsert_user_state(psid)
    log.info("[message] psid=%s ensured in user_state", psid)

    if text_lower in ("stats", "score", "how am i doing"):
        stats = db.get_stats(psid)
        log.info("[message] psid=%s stats: %s", psid, stats)
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
        log.info("[message] psid=%s resetting state", psid)
        db.upsert_user_state(psid, current_q=None, state="idle")
        msg.send_message(
            psid,
            "🔄 State reset. Your history is preserved but you can now get new questions. "
            "Type 'next' or wait for tomorrow's daily question!",
        )
        return

    if text_lower in ("next", "new question", "another", "skip"):
        log.info("[message] psid=%s requesting next question", psid)
        _send_daily_question(psid)
        return

    if text_lower in ("help", "start", "hi", "hello"):
        log.info("[message] psid=%s sent greeting — registering user", psid)
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
        db.upsert_user_state(psid, state="idle")
        log.info("[message] psid=%s registered in user_state", psid)
        return

    state = db.get_user_state(psid)
    current_q_num = state.get("current_q")
    user_state = state.get("state", "idle")
    log.info("[message] psid=%s state=%s current_q=%s", psid, user_state, current_q_num)

    if user_state == "awaiting_answer" and current_q_num:
        _handle_answer(psid, text, current_q_num)
    elif user_state == "answered" and current_q_num:
        q = get_question(current_q_num)
        _handle_followup(psid, text, q)
    elif current_q_num:
        q = get_question(current_q_num)
        _handle_followup(psid, text, q)
    else:
        _handle_followup(psid, text, None)


# ── Flask Routes ──────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Facebook webhook verification."""
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    log.info("[webhook] Verification attempt — mode=%s token_match=%s", mode, token == VERIFY_TOKEN)
    if mode == "subscribe" and token == VERIFY_TOKEN:
        log.info("[webhook] Verified successfully.")
        return challenge, 200
    log.warning("[webhook] Verification failed — token mismatch.")
    abort(403)


@app.route("/webhook", methods=["POST"])
def receive_message():
    """Handle incoming Messenger events."""
    data = request.get_json(force=True)
    log.info("[webhook] POST received — object=%s entries=%d",
             data.get("object"), len(data.get("entry", [])))

    if data.get("object") != "page":
        log.warning("[webhook] Unexpected object type: %s", data.get("object"))
        return "ok", 200

    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):
            psid = event.get("sender", {}).get("id")
            if not psid:
                continue

            if "message" in event:
                message = event["message"]
                if message.get("is_echo"):
                    continue
                text = message.get("text", "").strip()
                if text:
                    try:
                        _handle_message(psid, text)
                    except Exception as e:
                        log.error("[webhook] Error handling message from psid=%s: %s", psid, e, exc_info=True)
                        msg.send_message(psid, "⚠️ Something went wrong. Please try again.")

            elif "postback" in event:
                payload = event["postback"].get("payload", "")
                log.info("[webhook] Postback from psid=%s payload=%s", psid, payload)
                if payload:
                    try:
                        _handle_message(psid, payload)
                    except Exception as e:
                        log.error("[webhook] Error handling postback from psid=%s: %s", psid, e, exc_info=True)

    return "ok", 200


@app.route("/api/send-daily", methods=["GET", "POST"])
def trigger_daily():
    """
    Called by Vercel built-in cron (GET) or external cron like cron-job.org (POST).
    Optionally protected by a secret header (bypassed for Vercel's own cron).
    """
    user_agent = request.headers.get("User-Agent", "")
    is_vercel_cron = (
        request.headers.get("x-vercel-cron") == "1"
        or "vercel-cron" in user_agent.lower()
    )
    log.info("[cron] /api/send-daily called — method=%s is_vercel_cron=%s UA=%s",
             request.method, is_vercel_cron, user_agent)

    if CRON_SECRET and not is_vercel_cron:
        auth = request.headers.get("X-Cron-Secret", "")
        if auth != CRON_SECRET:
            log.warning("[cron] Unauthorized — bad or missing X-Cron-Secret")
            abort(401)

    result = _send_daily_question_to_all_users()
    log.info("[cron] Result: %s", result)
    return jsonify({"status": "ok", "message": "Daily questions sent.", "result": result})


@app.route("/api/status", methods=["GET"])
def status():
    questions = load_questions()
    log.info("[status] Health check — questions=%d db=%s", len(questions), bool(db.DATABASE_URL))
    return jsonify({
        "status": "running",
        "questions_loaded": len(questions),
        "llm_model": llm.MODEL,
        "database_url_set": bool(db.DATABASE_URL),
    })


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    startup()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
else:
    startup()
