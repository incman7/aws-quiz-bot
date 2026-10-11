"""
llm.py
Uses the Groq API (free tier) with Llama 3.3 70B to answer
follow-up questions about AWS SAA topics.

Conversation history is persisted in the database so multi-turn follow-up
questions survive serverless cold starts.
"""

import os
from groq import Groq
import database as db

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
MODEL = "llama-3.3-70b-versatile"   # fast, free tier, excellent quality

SYSTEM_PROMPT = """\
You are an expert AWS Solutions Architect tutor helping a student prepare for the
AWS Certified Solutions Architect – Associate (SAA-C03) exam.

When answering follow-up questions:
- Be concise but thorough (aim for 3-6 sentences)
- Use plain text (no markdown – this is a Messenger chat)
- Focus on practical AWS concepts and exam-relevant details
- If a concept is nuanced, highlight the key exam takeaway
- Do NOT re-ask the question or repeat the user's words unnecessarily
"""


def start_question_context(
    psid: str,
    question_text: str,
    correct_answer: str,
    explanation: str,
) -> None:
    """
    Called when a question result is sent. Seeds the conversation history
    for this user with the quiz context so follow-ups have full background.
    """
    context = (
        f"Quiz question context (for follow-up reference):\n"
        f"Question: {question_text}\n"
        f"Correct answer: {correct_answer}\n"
        f"Explanation: {explanation if explanation else '(none available)'}"
    )
    db.save_llm_history(psid, [
        {"role": "user", "content": context},
        {"role": "assistant", "content": "Got it. I'm ready to answer any follow-up questions about this question."},
    ])


def clear_history(psid: str) -> None:
    """Clear a user's conversation history (e.g. when a new question starts)."""
    db.clear_llm_history(psid)


def _chat(psid: str, user_text: str) -> str:
    """Common LLM call: load history, append turn, send to Groq, persist."""
    if not GROQ_API_KEY:
        return "⚠️ LLM not configured. Please set the GROQ_API_KEY environment variable."

    history = db.load_llm_history(psid)
    history.append({"role": "user", "content": user_text})
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    client = Groq(api_key=GROQ_API_KEY)
    try:
        chat = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_tokens=400,
            temperature=0.4,
        )
        reply = chat.choices[0].message.content.strip()
    except Exception as e:
        return f"⚠️ LLM error: {str(e)}"

    history.append({"role": "assistant", "content": reply})
    db.save_llm_history(psid, history)
    return reply


def answer_followup(psid: str, user_followup: str) -> str:
    """Continue the multi-turn conversation for this user's current question."""
    return _chat(psid, user_followup)


def answer_general(psid: str, user_message: str) -> str:
    """Answer a general AWS question (no active question context)."""
    return _chat(psid, user_message)