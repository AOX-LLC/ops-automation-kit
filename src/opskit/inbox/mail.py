"""Where inbound mail comes from: a two-method source, with a Mailpit implementation.

A Gmail adapter would implement the same two methods (`list_new` and `fetch`).
Only the plain-text part of a message is ever read. HTML is never rendered and
attachments are ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.utils import formataddr
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from pydantic import BaseModel

KIT_SENDER_SUFFIX = "@kit.example"
PAGE_SIZE = 50
MAX_PAGES = 40  # bounds one listing at 2,000 messages


@dataclass(frozen=True, slots=True)
class MailRef:
    mailpit_id: str
    message_id: str


class InboundMessage(BaseModel):
    message_id: str
    mailpit_id: str
    from_header: str
    reply_to_header: str | None = None
    to_addr: str | None = None
    subject: str
    received_at: datetime | None = None
    body_text: str


class MailSource(Protocol):
    async def list_new(self, *, known: set[str], limit: int) -> list[MailRef]: ...

    async def fetch(self, ref: MailRef) -> InboundMessage: ...


def _address(entry: Any) -> str:
    """One Mailpit address object ({"Name", "Address"}) as a header value."""
    if not isinstance(entry, dict):
        return ""
    address = str(entry.get("Address") or "")
    name = str(entry.get("Name") or "")
    return formataddr((name, address)) if address and name else address


def _address_list(entries: Any) -> str | None:
    if not isinstance(entries, list):
        return None
    joined = ", ".join(filter(None, (_address(e) for e in entries)))
    return joined or None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def sent_by_kit(raw: dict[str, Any]) -> bool:
    sender = raw.get("From")
    address = str(sender.get("Address") or "") if isinstance(sender, dict) else ""
    return address.lower().endswith(KIT_SENDER_SUFFIX)


class MailpitSource:
    """Mailpit's REST API: `GET /api/v1/messages` to list, `GET /api/v1/message/{ID}` to read."""

    def __init__(self, api_url: str, client: httpx.AsyncClient) -> None:
        self._api_url = api_url.rstrip("/")
        self._client = client

    async def list_new(self, *, known: set[str], limit: int) -> list[MailRef]:
        """Up to `limit` messages whose Message-ID is not in `known`, oldest first.

        Messages the kit sent itself (From ending `@kit.example`) are skipped.
        """
        found: dict[str, MailRef] = {}
        start = 0
        for _ in range(MAX_PAGES):
            response = await self._client.get(
                f"{self._api_url}/api/v1/messages", params={"start": start, "limit": PAGE_SIZE}
            )
            response.raise_for_status()
            body = response.json()
            page = body.get("messages") or []
            for raw in page:
                message_id = str(raw.get("MessageID") or "")
                if not message_id or message_id in known or sent_by_kit(raw):
                    continue
                found.setdefault(message_id, MailRef(str(raw.get("ID", "")), message_id))
            start += len(page)
            total = int(body.get("messages_count", body.get("total", 0)))
            if not page or start >= total:
                break
        # Mailpit lists newest first; work through the inbox oldest first.
        return list(reversed(list(found.values())))[:limit]

    async def fetch(self, ref: MailRef) -> InboundMessage:
        response = await self._client.get(
            f"{self._api_url}/api/v1/message/{quote(ref.mailpit_id, safe='')}"
        )
        response.raise_for_status()
        raw: dict[str, Any] = response.json()
        return InboundMessage(
            message_id=str(raw.get("MessageID") or ref.message_id),
            mailpit_id=ref.mailpit_id,
            from_header=_address(raw.get("From")),
            reply_to_header=_address_list(raw.get("ReplyTo")),
            to_addr=_address_list(raw.get("To")),
            subject=str(raw.get("Subject") or ""),
            received_at=_parse_time(raw.get("Date")) or _parse_time(raw.get("Created")),
            body_text=str(raw.get("Text") or ""),
        )
