"""Load the sample emails into Mailpit, once each."""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from email import policy
from email.parser import BytesParser
from pathlib import Path

import httpx

from opskit.config import Settings

TIMEOUT_S = 10.0


def read_headers(raw: bytes) -> tuple[str, str, str]:
    """Return (message_id, from, to); Message-ID is without angle brackets, as Mailpit stores it."""
    message = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    message_id = str(message["Message-ID"] or "").strip().strip("<>")
    sender = str(message["From"] or "")
    recipient = str(message["To"] or "")
    if not (message_id and sender and recipient):
        raise ValueError("email needs Message-ID, From and To headers")
    return message_id, sender, recipient


def search_query(message_id: str) -> str:
    # Quoted so ids containing special characters are matched as one term.
    escaped = message_id.replace("\\", "\\\\").replace('"', '\\"')
    return f'message-id:"{escaped}"'


def is_present(client: httpx.Client, api_url: str, message_id: str) -> bool:
    response = client.get(
        f"{api_url}/api/v1/search", params={"query": search_query(message_id), "limit": 1}
    )
    response.raise_for_status()
    body = response.json()
    return int(body.get("messages_count", len(body.get("messages", [])))) > 0


def _send_smtp(settings: Settings, sender: str, recipient: str, raw: bytes) -> None:
    with smtplib.SMTP(
        settings.mailpit_smtp_host, settings.mailpit_smtp_port, timeout=TIMEOUT_S
    ) as smtp:
        smtp.sendmail(sender, [recipient], raw)


def load_emails(
    settings: Settings,
    messages_dir: Path,
    *,
    client: httpx.Client | None = None,
    send: Callable[[Settings, str, str, bytes], None] = _send_smtp,
) -> tuple[int, int]:
    """Send each .eml that Mailpit does not already hold. Returns (sent, already_present)."""
    sent = already_present = 0
    owned = client is None
    http = client or httpx.Client(timeout=TIMEOUT_S)
    try:
        for eml in sorted(messages_dir.glob("*.eml")):
            raw = eml.read_bytes()
            message_id, sender, recipient = read_headers(raw)
            if is_present(http, settings.mailpit_api_url, message_id):
                already_present += 1
                continue
            send(settings, sender, recipient, raw)
            sent += 1
    finally:
        if owned:
            http.close()
    return sent, already_present
