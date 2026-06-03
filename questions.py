"""
questions.py
Loads AWS SAA-C03 questions from the bundled questions.json file.
Questions were extracted from the PDF (full A/B/C/D options) and
correct answers were matched from the solution TXT file.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Optional

_cache: list = []

QUESTIONS_FILE = os.path.join(os.path.dirname(__file__), "questions.json")


@dataclass
class Question:
    number: int
    text: str
    options: dict = field(default_factory=dict)   # {'A': '...', 'B': '...'}
    correct_answer: str = ""                       # 'A', 'B', 'C', or 'D'
    explanation: str = ""


def load_questions(force_reload: bool = False) -> list[Question]:
    """Return cached question list, loading from JSON if needed."""
    global _cache
    if _cache and not force_reload:
        return _cache
    with open(QUESTIONS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    _cache = [
        Question(
            number=q["number"],
            text=q["text"],
            options=q["options"],
            correct_answer=q.get("correct_answer", ""),
            explanation=q.get("explanation", ""),
        )
        for q in data
        if len(q.get("options", {})) >= 2
    ]
    print(f"[questions] Loaded {len(_cache)} questions from local file.")
    return _cache


def get_question(number: int) -> Optional[Question]:
    questions = load_questions()
    for q in questions:
        if q.number == number:
            return q
    return None


def format_question_message(q: Question) -> str:
    """Format a question for sending via Messenger."""
    lines = [f"\U0001f4da *Question {q.number}*\n"]
    lines.append(q.text)
    if q.options:
        lines.append("")
        for letter in sorted(q.options.keys()):
            lines.append(f"  {letter}. {q.options[letter]}")
    lines.append("\n\U0001f4ac Reply with the answer letter (A/B/C/D) or ask a question!")
    return "\n".join(lines)


def format_result_message(q: Question, user_answer: str) -> str:
    """Format the result after the user submits their answer."""
    correct = q.correct_answer.upper() if q.correct_answer else ""
    user = user_answer.upper()

    if correct:
        if user == correct:
            result = f"\u2705 Correct! The answer is *{correct}*."
        else:
            result = f"\u274c Not quite. You answered *{user}*, but the correct answer is *{correct}*."
    else:
        result = f"\U0001f4dd Your answer: *{user}*. (Correct answer not available for this question.)"

    lines = [result]
    if q.explanation:
        lines.append(f"\n\U0001f4d6 *Explanation:*\n{q.explanation}")
    lines.append("\n\U0001f4a1 Feel free to ask a follow-up question to dig deeper!")
    return "\n".join(lines)
