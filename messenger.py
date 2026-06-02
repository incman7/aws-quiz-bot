"""
messenger.py
Thin wrapper around the Facebook Messenger Send API.
"""

import os
import requests

PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN", "")
GRAPH_API_URL = "https://graph.facebook.com/v19.0/me/messages"


def send_message(recipient_psid: str, text: str) -> dict:
    """
    Send a plain-text message to a Messenger user.
    Facebook messages are limited to 2000 characters; we split if needed.
    """
    if not PAGE_ACCESS_TOKEN:
        raise RuntimeError("PAGE_ACCESS_TOKEN is not set.")

    chunks = _split_message(text, max_len=1900)
    results = []
    for chunk in chunks:
        payload = {
            "recipient": {"id": recipient_psid},
            "message": {"text": chunk},
            "messaging_type": "RESPONSE",
        }
        resp = requests.post(
            GRAPH_API_URL,
            params={"access_token": PAGE_ACCESS_TOKEN},
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        results.append(resp.json())
    return results[-1] if results else {}


def send_quick_replies(recipient_psid: str, text: str, replies: list[str]) -> dict:
    """
    Send a message with quick-reply buttons (e.g. A / B / C / D).
    Maximum 13 quick replies allowed by Facebook.
    """
    quick_reply_objects = [
        {"content_type": "text", "title": r, "payload": r}
        for r in replies[:13]
    ]
    payload = {
        "recipient": {"id": recipient_psid},
        "message": {
            "text": text[:1900],
            "quick_replies": quick_reply_objects,
        },
        "messaging_type": "RESPONSE",
    }
    resp = requests.post(
        GRAPH_API_URL,
        params={"access_token": PAGE_ACCESS_TOKEN},
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def send_typing_on(recipient_psid: str):
    """Show 'typing...' indicator."""
    requests.post(
        GRAPH_API_URL,
        params={"access_token": PAGE_ACCESS_TOKEN},
        json={
            "recipient": {"id": recipient_psid},
            "sender_action": "typing_on",
        },
        timeout=5,
    )


def _split_message(text: str, max_len: int = 1900) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # Try to split at a newline or space
        split_at = text.rfind('\n', 0, max_len)
        if split_at == -1:
            split_at = text.rfind(' ', 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at].strip())
        text = text[split_at:].strip()
    return chunks
