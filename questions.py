"""
questions.py
Fetches and parses AWS SAA-C03 questions from the GitHub text file.
Questions are cached in memory after first load.
"""

import re
import requests
from dataclasses import dataclass, field
from typing import Optional

QUESTIONS_URL = (
    "https://raw.githubusercontent.com/Iamrushabhshahh/"
    "AWS-Certified-Solutions-Architect-Associate-SAA-C03-Exam-Dump-With-Solution/"
    "main/AWS%20SAA-03%20Solution.txt"
)

_cache: list = []


@dataclass
class Question:
    number: int
    text: str
    options: dict = field(default_factory=dict)   # {'A': '...', 'B': '...'}
    correct_answer: str = ""                       # 'A', 'B', 'C', or 'D'
    explanation: str = ""


def _fetch_raw() -> str:
    resp = requests.get(QUESTIONS_URL, timeout=30)
    resp.raise_for_status()
    return resp.text


def _parse_options(block: str) -> dict:
    """Extract answer options A/B/C/D/E from a text block."""
    options = {}
    # Match lines like "A. Some text" or "A) Some text"
    pattern = re.compile(
        r'^\s*([A-E])[.)][ \t]+(.+?)(?=\n\s*[A-E][.)]\s|\Z)',
        re.MULTILINE | re.DOTALL,
    )
    for m in pattern.finditer(block):
        letter = m.group(1)
        text = re.sub(r'\s+', ' ', m.group(2)).strip()
        options[letter] = text
    return options


def _parse_correct_answer(block: str) -> str:
    """
    Try several heuristics to find the correct answer letter.
    Returns '' if not determinable.
    """
    # Pattern 1: Explicit "Answer: B)" or "ans- B" or "Correct answer: B"
    m = re.search(
        r'(?:answer[:\s\-]+|ans[-:\s]+|correct answer[:\s]+)([A-E])',
        block, re.IGNORECASE,
    )
    if m:
        return m.group(1).upper()

    # Pattern 2: "Option B:" or "Option B is..."
    m = re.search(r'\bOption\s+([A-E])\b', block, re.IGNORECASE)
    if m:
        return m.group(1).upper()

    # Pattern 3: A standalone line like "B. Configure..." right after the question
    # (i.e., only one option letter is given = that's the answer)
    letters_found = re.findall(r'^\s*([A-E])[.)]\s', block, re.MULTILINE)
    unique = list(dict.fromkeys(letters_found))  # preserve order, dedupe
    if len(unique) == 1:
        return unique[0].upper()

    # Pattern 4: Last letter before the explanation separator
    # e.g. "D. Use Amazon RDS..." as the final answer statement
    all_matches = re.findall(r'([A-E])[.)]\s', block)
    if all_matches:
        return all_matches[0].upper()

    return ""


def _clean(text: str) -> str:
    """Collapse excessive whitespace / newlines."""
    text = re.sub(r'\r\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _parse(raw: str) -> list[Question]:
    """Split raw text into Question objects."""
    # Separator is a long run of dashes/equals
    separator = re.compile(r'[-=]{10,}')
    chunks = separator.split(raw)

    questions = []
    # Pattern for question headers: "8]" or "IMP>>>8]" etc.
    q_header = re.compile(r'(?:IMP[^]]*)?(\d{1,3})\]\s*', re.IGNORECASE)

    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        m = q_header.search(chunk)
        if not m:
            continue

        number = int(m.group(1))
        body = chunk[m.start():]  # everything from the number onward

        # Split body at the first blank line to separate question from answer/explanation
        parts = re.split(r'\n\n+', body, maxsplit=1)
        question_part = parts[0] if parts else body
        rest = parts[1] if len(parts) > 1 else ""

        # Remove the "N]" prefix from the question text
        question_text = q_header.sub('', question_part, count=1).strip()
        question_text = _clean(question_text)

        options = _parse_options(body)
        correct = _parse_correct_answer(body)

        # Explanation: everything after the correct answer statement
        explanation = _clean(rest) if rest else ""
        # Trim explanation to a readable length
        if len(explanation) > 1200:
            explanation = explanation[:1200].rsplit(' ', 1)[0] + "..."

        if question_text and number > 0:
            questions.append(
                Question(
                    number=number,
                    text=question_text,
                    options=options,
                    correct_answer=correct,
                    explanation=explanation,
                )
            )

    # Dedupe by number (keep last occurrence, which tends to be more complete)
    seen = {}
    for q in questions:
        seen[q.number] = q
    result = sorted(seen.values(), key=lambda q: q.number)
    return result


def load_questions(force_reload: bool = False) -> list[Question]:
    """Return cached question list, fetching/parsing if needed."""
    global _cache
    if _cache and not force_reload:
        return _cache
    raw = _fetch_raw()
    _cache = _parse(raw)
    print(f"[questions] Loaded {len(_cache)} questions.")
    return _cache


def get_question(number: int) -> Optional[Question]:
    questions = load_questions()
    for q in questions:
        if q.number == number:
            return q
    return None


def format_question_message(q: Question) -> str:
    """Format a question for sending via Messenger."""
    lines = [f"📚 *Question {q.number}*\n"]
    lines.append(q.text)
    if q.options:
        lines.append("")
        for letter in sorted(q.options.keys()):
            lines.append(f"  {letter}. {q.options[letter]}")
    lines.append("\n💬 Reply with the answer letter (A/B/C/D) or ask a question!")
    return "\n".join(lines)


def format_result_message(q: Question, user_answer: str) -> str:
    """Format the result after the user submits their answer."""
    correct = q.correct_answer.upper()
    user = user_answer.upper()

    if correct:
        if user == correct:
            result = f"✅ Correct! The answer is *{correct}*."
        else:
            result = f"❌ Not quite. You answered *{user}*, but the correct answer is *{correct}*."
    else:
        result = f"📝 Your answer: *{user}*. (Correct answer data not available for this question.)"

    lines = [result]
    if q.explanation:
        lines.append(f"\n📖 *Explanation:*\n{q.explanation}")
    lines.append("\n💡 Feel free to ask a follow-up question to dig deeper!")
    return "\n".join(lines)
