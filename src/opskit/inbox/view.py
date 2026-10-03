"""What the approver page shows for an `inbox.send_reply` request.

The reply itself comes from the approval's own payload (the exact text bound to the
approval); the original message, its triage label and the grounding report come from
the stored draft the payload names. Email content is untrusted: the template escapes it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from opskit.db.engine import SessionFactory
from opskit.inbox import store

log = logging.getLogger(__name__)
# What a stored row of the wrong shape raises: a model refusing it, or a column of the wrong type.
_UNPARSABLE = (ValueError, TypeError, KeyError)


@dataclass(frozen=True, slots=True)
class InboxReplyView:
    original_from: str
    original_subject: str
    original_received_at: datetime | None
    original_body: str
    category: str
    priority: str
    to: str
    subject: str
    body: str
    facts_used: list[str]
    commitment_flags: list[str]
    reply_to_differs: bool


def _text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


async def load_inbox_reply_view(
    session_factory: SessionFactory, approval_id: UUID, payload: Mapping[str, Any]
) -> InboxReplyView | None:
    """The view for an approval's payload, or None if its draft or message is missing.

    Only the approval the draft itself points at gets this view. Any other approval that
    names the draft (one made through the generic route, say) is shown as plain JSON, so it
    can never pose as "the reply, exactly as it will be sent".
    """
    try:
        draft_id = UUID(_text(payload, "draft_id"))
    except ValueError:
        return None
    try:
        draft = await store.get_draft(session_factory, draft_id)
        if draft is None or draft.approval_id != approval_id:
            return None
        message = await store.load_message(session_factory, draft.message_id)
        triage = await store.load_triage(session_factory, draft.message_id)
    except _UNPARSABLE:
        # A stored row of the wrong shape: show the approval as plain JSON, which is all the
        # approver needs to decide on, rather than failing the page.
        log.error(
            "inbox draft %s could not be read; showing the approval without its view", draft_id
        )
        return None
    if message is None or triage is None:
        return None
    flags = draft.grounding.get("commitment_flags")
    return InboxReplyView(
        original_from=message.from_header,
        original_subject=message.subject,
        original_received_at=message.received_at,
        original_body=message.body_text,
        category=triage.category,
        priority=triage.priority,
        to=_text(payload, "to"),
        subject=_text(payload, "subject"),
        body=_text(payload, "body"),
        facts_used=draft.facts_used,
        commitment_flags=[str(f) for f in flags] if isinstance(flags, list) else [],
        reply_to_differs=draft.reply_to_differs,
    )
