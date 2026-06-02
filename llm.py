"""
llm.py
Uses the Groq API (free tier) with Llama 3.3 70B to answer
follow-up questions about AWS SAA topics.
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


def answer_followup(
    question_text: str,
    correct_answer: str,
    explanation: str,
    user_followup: str,
) -> str:
    """
    Given the context of the current quiz question and the user's follow-up,
    return a concise LLM-generated answer.
    """
    if not GROQ_API_KEY:
        return (
            "⚠️ LLM not configured. Please set the GROQ_API_KEY environment variable."
        )

    client = Groq(api_key=GROQ_API_KEY)

    context = f"""
CURRENT QUIZ QUESTION:
{question_text}

CORRECT ANSWER: {correct_answer}

EXPLANATION FROM STUDY MATERIAL:
{explanation if explanation else '(No explanation available)'}

STUDENT'S FOLLOW-UP QUESTION:
{user_followup}
""".strip()

    try:
        chat = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": context},
            ],
            max_tokens=400,
            temperature=0.4,
        )
        return chat.choices[0].message.content.strip()
    except Exception as e:
        return f"⚠️ LLM error: {str(e)}"


def answer_general(user_message: str) -> str:
    """
    Answer a general AWS question when not in the context of a specific question.
    """
    if not GROQ_API_KEY:
        return (
            "⚠️ LLM not configured. Please set the GROQ_API_KEY environment variable."
        )

    client = Groq(api_key=GROQ_API_KEY)
    try:
        chat = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            max_tokens=400,
            temperature=0.4,
        )
        return chat.choices[0].message.content.strip()
    except Exception as e:
        return f"⚠️ LLM error: {str(e)}"
