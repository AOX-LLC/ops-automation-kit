"""What a fetched message is cut down to before it is stored.

Mail is untrusted input. The database refuses an oversize or out-of-range row, so one hostile
email would fail the whole fetch; clamping here keeps ingestion alive instead.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # mail imports this module, so a runtime import would be a cycle
    from opskit.inbox.mail import InboundMessage

log = logging.getLogger(__name__)

# These mirror the inbox_0003 migration's bounds; a test cross-checks them.
MESSAGE_ID_MAX = 998
MAILPIT_ID_MAX = 200
HEADER_MAX = 1000
ADDRESS_LIST_MAX = 2000
SUBJECT_MAX = 2000
BODY_MAX = 1_048_576
RECEIVED_MIN = datetime(1990, 1, 1, tzinfo=UTC)
RECEIVED_MAX = datetime(2100, 1, 1, tzinfo=UTC)


def identity_storable(message_id: str, mailpit_id: str) -> bool:
    """Whether a message with these ids can be stored as they are. An id is never cut or cleaned:
    a rewritten one could equal another message's and overwrite it, so it is refused instead."""
    return (
        1 <= len(message_id) <= MESSAGE_ID_MAX
        and 1 <= len(mailpit_id) <= MAILPIT_ID_MAX
        and _clean(message_id) == message_id
        and _clean(mailpit_id) == mailpit_id
    )


def _identity_fits(field: str, value: str, limit: int) -> bool:
    """False, with a content-free warning, when an identifier cannot be stored as it is."""
    if 1 <= len(value) <= limit and _clean(value) == value:
        return True
    log.warning(
        "inbox message skipped: %s has length %d (allowed 1..%d, no NUL or lone surrogate)",
        field,
        len(value),
        limit,
    )
    return False


def _clean(value: str) -> str:
    """Text the database can hold: no NUL (Postgres text cannot contain one) and no lone
    surrogate (which cannot be encoded); a lone surrogate becomes '?'."""
    return value.replace("\x00", "").encode("utf-8", "replace").decode("utf-8")


def _text(value: str | None, limit: int) -> str | None:
    return value if value is None else _clean(value)[:limit]


def _received_in_range(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return value if RECEIVED_MIN <= aware <= RECEIVED_MAX else None


def clamp_message(msg: InboundMessage) -> InboundMessage | None:
    """The message as it can be stored, or None when its identity cannot be stored.

    Text is cut by characters; an out-of-range `received_at` becomes None. An identifier is
    never cut (a cut one would name a different message), so such a message is skipped.
    """
    if not (
        _identity_fits("message_id", msg.message_id, MESSAGE_ID_MAX)
        and _identity_fits("mailpit_id", msg.mailpit_id, MAILPIT_ID_MAX)
    ):
        return None
    return msg.model_copy(
        update={
            "from_header": _clean(msg.from_header)[:HEADER_MAX],
            "reply_to_header": _text(msg.reply_to_header, ADDRESS_LIST_MAX),
            "to_addr": _text(msg.to_addr, ADDRESS_LIST_MAX),
            "subject": _clean(msg.subject)[:SUBJECT_MAX],
            "received_at": _received_in_range(msg.received_at),
            "body_text": _clean(msg.body_text)[:BODY_MAX],
        }
    )
