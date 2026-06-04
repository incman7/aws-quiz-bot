"""
llm.py
Uses the Groq API (free tier) with Llama 3.3 70B to answer
follow-up questions about AWS SAA topics.

Maintains per-user conversation history so multi-turn follow-up
questions work correctly within a question session.
"""

import os
from groq import Groq

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

# Per-user conversation history: {psid: [{"role": ..., "content": ...}, ...]}
_conversation_history: dict[str, list] = {}


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
    _conversation_history[psid] = [
        {"role": "user", "content": context},
        {"role": "assistant", "content": "Got it. I'm ready to answer any follow-up questions about this question."},
    ]


def clear_history(psid: str) -> None:
    """Clear a user's conversation history (e.g. when a new question starts)."""
    _conversation_history.pop(psid, None)


def answer_followup(psid: str, user_followup: str) -> str:
    """
    Continue the multi-turn conversation for this user's current question.
    History is maintained between calls so follow-up questions have context.
    """
    if not GROQ_API_KEY:
        return "⚠️ LLM not configured. Please set the GROQ_API_KEY environment variable."

    client = Groq(api_key=GROQ_API_KEY)

    history = _conversation_history.get(psid, [])
    history.append({"role": "user", "content": user_followup})

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    try:
        chat = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_tokens=400,
            temperature=0.4,
        )
        reply = chat.choices[0].message.content.strip()
        history.append({"role": "assistant", "content": reply})
        _conversation_history[psid] = history
        return reply
    except Exception as e:
        return f"⚠️ LLM error: {str(e)}"


def answer_general(psid: str, user_message: str) -> str:
    """
    Answer a general AWS question (no active question context).
    Also maintains conversation history for back-and-forth.
    """
    if not GROQ_API_KEY:
        return "⚠️ LLM not configured. Please set the GROQ_API_KEY environment variable."

    client = Groq(api_key=GROQ_API_KEY)

    history = _conversation_history.get(psid, [])
    history.append({"role": "user", "content": user_message})

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    try:
        chat = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            max_tokens=400,
            temperature=0.4,
        )
        reply = chat.choices[0].message.content.strip()
        history.append({"role": "assistant", "content": reply})
        _conversation_history[psid] = history
        return reply
    except Exception as e:
        return f"⚠️ LLM error: {str(e)}"
