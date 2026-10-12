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
from datetime import date

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

# Tag for daily-question sends. Outside the 24h user-message window, Facebook
# requires a message tag. NON_PROMOTIONAL_SUBSCRIPTION is the right one for
# recurring content (daily quiz) — REQUIRES the user to have explicitly
# opted in via the bot (we do that with a Subscribe quick-reply button in the
# welcome message). Sending without opt-in causes Facebook to silently
# reject the message with a "user has not subscribed" error.
DAILY_TAG = "NON_PROMOTIONAL_SUBSCRIPTION"


def _extract_fb_error(e: Exception) -> str:
    """Best-effort extraction of Facebook's error message from a request exception."""
    resp = getattr(e, "response", None)
    if resp is not None:
        try:
            body = resp.json()
            err = body.get("error", {})
            sub = err.get("error_subcode")
            msg = err.get("message") or str(e)
            code = resp.status_code
            return f"HTTP {code} (subcode={sub}): {msg}"
        except Exception:
            try:
                return f"HTTP {resp.status_code}: {resp.text[:200]}"
            except Exception:
                pass
    return str(e)


def _pick_next_question(psid: str):
    """Pick a question the user hasn't answered yet (random order)."""
    questions = load_questions()
    answered = db.get_answered_questions(psid)
    remaining = [q for q in questions if q.number not in answered and len(q.options) >= 2]
    log.info("[pick_question] psid=%s total=%d answered=%d remaining=%d",
             psid, len(questions), len(answered), len(remaining))
    if not remaining:
        return None
    return random.choice(remaining)


def _send_daily_question_to_all_users():
    """Send the daily question to every opted-in user who hasn't already received one today."""
    today_iso = date.today().isoformat()
    log.info("[cron] Starting daily send. today=%s DATABASE_URL set: %s",
             today_iso, bool(db.DATABASE_URL))

    # Idempotency + opt-in filter: only send to subscribed users who haven't
    # been sent today's question yet.
    try:
        psids = db.get_users_due_for_question(today_iso)
        log.info("[cron] %d user(s) due for today's question (subscribed & not yet sent)", len(psids))
    except Exception as e:
        log.error("[cron] DB error fetching users: %s", e, exc_info=True)
        return {"psids_found": 0, "db_error": str(e)}

    if not psids:
        # Distinguish "no subscribers" from "everyone already got today" by
        # checking the full table.
        try:
            total = len(db.list_users_for_debug())
            subscribed = sum(1 for u in db.list_users_for_debug() if u.get("subscribed"))
            log.warning("[cron] Nothing to send. total_users=%d subscribed=%d",
                        total, subscribed)
        except Exception:
            pass

    errors = []
    sent = 0
    for psid in psids:
        try:
            log.info("[cron] Sending question to psid=%s", psid)
            _send_daily_question(psid)
            db.mark_question_sent(psid, today_iso)
            sent += 1
            log.info("[cron] Successfully sent to psid=%s", psid)
        except Exception as e:
            detail = _extract_fb_error(e)
            log.error("[cron] Failed to send to psid=%s: %s", psid, detail, exc_info=True)
            errors.append({"psid": psid, "error": detail})

    log.info("[cron] Done. sent=%d errors=%d", sent, len(errors))
    return {"psids_found": len(psids), "sent": sent, "errors": errors}


def _send_daily_question(psid: str):
    """Send a new question to one user. Uses the NON_PROMOTIONAL_SUBSCRIPTION
    tag so Facebook allows the message outside the 24h response window."""
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
    llm.clear_history(psid)
    db.upsert_user_state(psid, current_q=q.number, state="awaiting_answer")

    question_msg = format_question_message(q)
    if q.options:
        letters = sorted(q.options.keys())
        msg.send_quick_replies(psid, question_msg, letters, tag=DAILY_TAG)
    else:
        msg.send_message(psid, question_msg, tag=DAILY_TAG)


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

    # Streak: increment if consecutive-day, reset on gap.
    new_streak = db.update_streak_on_answer(psid, date.today().isoformat())
    log.info("[answer] psid=%s new streak=%d", psid, new_streak)

    result_msg = format_result_message(q, answer_letter)
    if new_streak > 1:
        result_msg += f"\n🔥 Current streak: {new_streak} day(s)."
    msg.send_message(psid, result_msg)

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
        streak = stats.get("streak", 0)
        streak_line = f"🔥 Current streak: {streak} day(s)\n" if streak else ""
        msg.send_message(
            psid,
            f"📊 Your stats:\n"
            f"  Questions answered: {stats['total']}\n"
            f"  Correct: {stats['correct']}\n"
            f"  Score: {stats['percentage']}%\n"
            f"{streak_line}\n"
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
        # If today's question is still unanswered, gently nudge the user back to it
        # instead of replacing it with a new one — unless they explicitly say 'skip'.
        if text_lower != "skip":
            state = db.get_user_state(psid)
            today_iso = date.today().isoformat()
            if state.get("last_sent_date") == today_iso and state.get("current_q"):
                answered_today = db.has_answered_today(psid)
                if not answered_today:
                    msg.send_message(
                        psid,
                        f"You still have today's question pending (Q{state['current_q']}). "
                        f"Reply with A/B/C/D to answer it, or type 'skip' to move on.",
                    )
                    return
        _send_daily_question(psid)
        return

    if text_lower in ("subscribe", "daily", "yes", "enable"):
        log.info("[message] psid=%s opted in to daily reminders", psid)
        db.set_subscribed(psid, True)
        msg.send_message(
            psid,
            "✅ Subscribed! You'll get one quiz question every day at 9am AWST.\n\n"
            "💬 Reply with A / B / C / D to answer.\n"
            "🤔 Ask a follow-up question anytime for an AI explanation.\n\n"
            "Reply 'unsubscribe' to stop daily reminders.",
        )
        return

    if text_lower in ("unsubscribe", "stop", "off", "cancel"):
        log.info("[message] psid=%s opted out of daily reminders", psid)
        db.set_subscribed(psid, False)
        msg.send_message(
            psid,
            "🔕 Unsubscribed. You won't get daily reminders anymore.\n\n"
            "You can still type 'next' for a question anytime.\n"
            "Reply 'subscribe' to opt back in.",
        )
        return

    if text_lower in ("help", "start", "hi", "hello"):
        log.info("[message] psid=%s sent greeting — registering user", psid)
        # Welcome message includes a Subscribe button so the user can opt in
        # in one tap. Without opt-in, Facebook rejects our daily sends.
        msg.send_quick_replies(
            psid,
            "👋 Welcome to AWS SAA-C03 Daily Quiz Bot!\n\n"
            "📅 Tap *Subscribe* to receive one question every day at 9am AWST.\n"
            "💬 Reply with A / B / C / D to answer.\n"
            "🤔 Ask any follow-up question and I'll explain using AI.\n\n"
            "Commands:\n"
            "  next  – Get a question now\n"
            "  skip  – Skip to the next one\n"
            "  stats – See your score & streak\n"
            "  unsubscribe – Stop daily reminders\n"
            "  help  – Show this message",
            ["Subscribe", "Help"],
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
                        # Never let a fallback send raise — Facebook expects a 200,
                        # and if our error message itself fails (e.g. token rotation,
                        # network blip) we'd 500 the webhook and Facebook may disable it.
                        try:
                            msg.send_message(psid, "⚠️ Something went wrong. Please try again.")
                        except Exception as send_err:
                            log.error("[webhook] Failed to send error message to psid=%s: %s",
                                      psid, send_err)

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
    users = []
    try:
        users = db.list_users_for_debug()
    except Exception as e:
        log.error("[status] Could not list users: %s", e)
    subscribed = sum(1 for u in users if u.get("subscribed"))
    db_type = "postgres" if db.DATABASE_URL else "sqlite"
    log.info("[status] Health check — questions=%d users=%d subscribed=%d db=%s",
             len(questions), len(users), subscribed, db_type)
    return jsonify({
        "status": "running",
        "questions_loaded": len(questions),
        "llm_model": llm.MODEL,
        "database_type": db_type,
        "users_total": len(users),
        "users_subscribed": subscribed,
    })


@app.route("/api/debug/users", methods=["GET"])
def debug_users():
    """List all users — masked PSIDs + state. For diagnostics when logs aren't available."""
    try:
        users = db.list_users_for_debug()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    masked = []
    for u in users:
        psid = u.get("psid", "")
        masked.append({
            "psid_masked": psid[:4] + "…" + psid[-2:] if len(psid) > 6 else psid,
            "psid": psid,
            "current_q": u.get("current_q"),
            "state": u.get("state"),
            "subscribed": bool(u.get("subscribed")),
            "streak": u.get("streak_count"),
            "last_sent_date": u.get("last_sent_date"),
            "last_answered_date": u.get("last_answered_date"),
            "last_active": u.get("last_active"),
        })
    return jsonify({"today": date.today().isoformat(), "users": masked})


@app.route("/api/debug/state", methods=["GET"])
def debug_state():
    """Return full state for one PSID. ?psid=... (required)."""
    psid = request.args.get("psid", "").strip()
    if not psid:
        return jsonify({"error": "psid query param required"}), 400
    state = db.get_user_state(psid)
    answered = sorted(db.get_answered_questions(psid))
    return jsonify({
        "psid": psid,
        "state": state,
        "answered_count": len(answered),
        "answered_sample": answered[:10],
    })


@app.route("/api/debug/trigger-send", methods=["GET", "POST"])
def debug_trigger_send():
    """
    Manually trigger the daily-send flow for one (or all) users.
    Bypasses the CRON_SECRET check (use only for diagnostics).
    ?psid=... optional — if omitted, sends to all due users.
    Returns the full result including per-user Facebook error details.
    """
    psid = request.args.get("psid", "").strip()
    today_iso = date.today().isoformat()

    if psid:
        # Single-user send: bypass idempotency so you can re-test.
        log.info("[debug] Manual trigger for psid=%s", psid)
        try:
            _send_daily_question(psid)
            db.mark_question_sent(psid, today_iso)
            return jsonify({"status": "ok", "psid": psid, "sent": True})
        except Exception as e:
            return jsonify({
                "status": "error",
                "psid": psid,
                "error": _extract_fb_error(e),
            }), 500

    # No psid: do the full cron flow (idempotent).
    result = _send_daily_question_to_all_users()
    return jsonify({"status": "ok", "today": today_iso, "result": result})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    startup()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
else:
    startup()