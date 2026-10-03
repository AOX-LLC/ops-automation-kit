"""Persistence for fetched messages, triage results and drafts (schema `inbox`)."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from opskit.db.engine import SessionFactory
from opskit.db.tables import inbox_drafts as drafts
from opskit.db.tables import inbox_messages as messages
from opskit.db.tables import inbox_triage as triage_table
from opskit.inbox.mail import InboundMessage
from opskit.inbox.policy import Route
from opskit.inbox.service import (
    DraftOutcome,
    InjectionReason,
    TriageOutcome,
    reply_to_differs,
)


class DraftConflict(Exception):
    """A draft already exists for this message; drafts are insert-once."""


class StoredDraft(BaseModel):
    draft_id: UUID
    message_id: str
    run_id: UUID
    approval_id: UUID | None
    to: str
    subject: str
    in_reply_to: str | None
    body: str
    facts_used: list[str]
    grounding: dict[str, Any]
    reply_to_differs: bool
    status: str
    failure_reason: str | None
    sent_at: datetime | None


def approval_payload(draft: StoredDraft) -> dict[str, str]:
    """What an inbox.send_reply approval covers: exactly the envelope that will be sent.

    Built in one place, so the payload submitted for approval and the payload checked at
    release (agent-core binds the approval to its hash) can never drift apart.
    """
    return {
        "draft_id": str(draft.draft_id),
        "to": draft.to,
        "subject": draft.subject,
        "in_reply_to": draft.in_reply_to or "",
        "body": draft.body,
    }


class QuarantinedItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    message_id: str
    sender: str = Field(serialization_alias="from")
    subject: str
    category: str
    evidence: str | None


class InboxSummary(BaseModel):
    counts: dict[str, int]
    routes: dict[str, int]
    drafts: dict[str, int]
    quarantined: list[QuarantinedItem]


# --- messages -----------------------------------------------------------------------------


async def save_message(session_factory: SessionFactory, msg: InboundMessage) -> None:
    """Insert the message, or refresh its fetched copy."""
    values = {
        "message_id": msg.message_id,
        "mailpit_id": msg.mailpit_id,
        "from_header": msg.from_header,
        "reply_to_header": msg.reply_to_header,
        "to_addr": msg.to_addr,
        "subject": msg.subject,
        "received_at": msg.received_at,
        "body_text": msg.body_text,
    }
    statement = insert(messages).values(values)
    statement = statement.on_conflict_do_update(
        index_elements=[messages.c.message_id],
        set_={name: statement.excluded[name] for name in values if name != "message_id"},
    )
    async with session_factory() as session, session.begin():
        await session.execute(statement)


async def load_message(session_factory: SessionFactory, message_id: str) -> InboundMessage | None:
    async with session_factory() as session:
        row = (
            await session.execute(select(messages).where(messages.c.message_id == message_id))
        ).first()
    return _message_of(row) if row is not None else None


def _message_of(row: Any) -> InboundMessage:
    return InboundMessage(
        message_id=row.message_id,
        mailpit_id=row.mailpit_id,
        from_header=row.from_header,
        reply_to_header=row.reply_to_header,
        to_addr=row.to_addr,
        subject=row.subject,
        received_at=row.received_at,
        body_text=row.body_text,
    )


async def known_message_ids(session_factory: SessionFactory) -> set[str]:
    async with session_factory() as session:
        return set((await session.execute(select(messages.c.message_id))).scalars())


# --- triage -------------------------------------------------------------------------------


async def save_triage(
    session_factory: SessionFactory, run_id: UUID, outcome: TriageOutcome
) -> TriageOutcome:
    """Store a message's triage once and return what is stored.

    Two runs can triage the same message at once, and a live model can disagree with
    itself. The first result stands, except that a hold always wins: if either result
    quarantined the message it stays quarantined, so a later, calmer answer can never
    release a message the earlier one held.
    """
    values: dict[str, Any] = {
        "message_id": outcome.message_id,
        "run_id": run_id,
        "category": outcome.category,
        "priority": outcome.priority,
        "needs_reply": outcome.needs_reply,
        "escalate": outcome.escalate,
        "route": outcome.route,
        "quarantined": outcome.quarantined,
        "injection_reasons": [r.model_dump() for r in outcome.injection_reasons],
        "replay_key": outcome.replay_key,
        "cost_usd": Decimal(outcome.cost_usd),
        "latency_ms": outcome.latency_ms,
    }
    statement = insert(triage_table).values(values)
    stored, incoming = triage_table.c, statement.excluded
    statement = statement.on_conflict_do_update(
        index_elements=[stored.message_id],
        set_={
            "quarantined": or_(stored.quarantined, incoming.quarantined),
            "route": case(
                (or_(stored.quarantined, incoming.quarantined), Route.QUARANTINE.value),
                else_=stored.route,
            ),
            "injection_reasons": case(
                (stored.quarantined, stored.injection_reasons),
                else_=incoming.injection_reasons,
            ),
        },
    )
    async with session_factory() as session, session.begin():
        await session.execute(statement)
    saved = await load_triage(session_factory, outcome.message_id)
    if saved is None:
        raise RuntimeError(f"triage for {outcome.message_id} vanished after it was written")
    return saved


async def is_quarantined(session_factory: SessionFactory, message_id: str) -> bool:
    async with session_factory() as session:
        held = (
            await session.execute(
                select(triage_table.c.quarantined).where(triage_table.c.message_id == message_id)
            )
        ).scalar_one_or_none()
    return bool(held)


def _triage_of(row: Any, msg: InboundMessage) -> TriageOutcome:
    return TriageOutcome(
        message_id=row.message_id,
        category=row.category,
        priority=row.priority,
        needs_reply=row.needs_reply,
        escalate=row.escalate,
        route=row.route,
        quarantined=row.quarantined,
        injection_reasons=[InjectionReason.model_validate(r) for r in row.injection_reasons],
        reply_to_differs=reply_to_differs(msg),
        replay_key=row.replay_key.strip() if row.replay_key else None,
        cost_usd=format(row.cost_usd, "f"),
        latency_ms=row.latency_ms,
    )


async def load_triage(session_factory: SessionFactory, message_id: str) -> TriageOutcome | None:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(triage_table).where(triage_table.c.message_id == message_id)
            )
        ).first()
        message_row = (
            await session.execute(select(messages).where(messages.c.message_id == message_id))
        ).first()
    if row is None or message_row is None:
        return None
    return _triage_of(row, _message_of(message_row))


async def triaged_ids(session_factory: SessionFactory) -> set[str]:
    async with session_factory() as session:
        return set((await session.execute(select(triage_table.c.message_id))).scalars())


# --- drafts -------------------------------------------------------------------------------


async def save_draft(
    session_factory: SessionFactory, run_id: UUID, message_id: str, outcome: DraftOutcome
) -> UUID:
    """Insert the draft once per message; a second one raises DraftConflict."""
    statement = (
        insert(drafts)
        .values(
            message_id=message_id,
            run_id=run_id,
            to_addr=outcome.to,
            subject=outcome.subject,
            in_reply_to=outcome.in_reply_to,
            body=outcome.body,
            facts_used=outcome.facts_used,
            grounding=outcome.grounding,
            reply_to_differs=outcome.reply_to_differs,
            status=outcome.status,
            failure_reason=outcome.failure_reason,
            replay_key=outcome.replay_key,
            cost_usd=Decimal(outcome.cost_usd),
            latency_ms=outcome.latency_ms,
        )
        .on_conflict_do_nothing(index_elements=[drafts.c.message_id])
        .returning(drafts.c.id)
    )
    async with session_factory() as session, session.begin():
        draft_id: UUID | None = (await session.execute(statement)).scalar_one_or_none()
    if draft_id is None:
        raise DraftConflict(message_id)
    return draft_id


def _draft_of(row: Any) -> StoredDraft:
    return StoredDraft(
        draft_id=row.id,
        message_id=row.message_id,
        run_id=row.run_id,
        approval_id=row.approval_id,
        to=row.to_addr,
        subject=row.subject,
        in_reply_to=row.in_reply_to,
        body=row.body,
        facts_used=list(row.facts_used),
        grounding=dict(row.grounding),
        reply_to_differs=row.reply_to_differs,
        status=row.status,
        failure_reason=row.failure_reason,
        sent_at=row.sent_at,
    )


async def get_draft(session_factory: SessionFactory, draft_id: UUID) -> StoredDraft | None:
    async with session_factory() as session:
        row = (await session.execute(select(drafts).where(drafts.c.id == draft_id))).first()
    return _draft_of(row) if row is not None else None


async def get_draft_for_message(
    session_factory: SessionFactory, message_id: str
) -> StoredDraft | None:
    async with session_factory() as session:
        row = (
            await session.execute(select(drafts).where(drafts.c.message_id == message_id))
        ).first()
    return _draft_of(row) if row is not None else None


async def set_draft_status(
    session_factory: SessionFactory,
    draft_id: UUID,
    *,
    from_statuses: tuple[str, ...],
    to_status: str,
    approval_id: UUID | None = None,
    sent: bool = False,
) -> bool:
    """Move a draft between statuses only if it is in one of `from_statuses` right now.

    Returns whether a row changed, so two callers racing for the same move cannot both win.
    """
    values: dict[str, Any] = {"status": to_status}
    if approval_id is not None:
        values["approval_id"] = approval_id
    if sent:
        values["sent_at"] = datetime.now(UTC)
    statement = (
        update(drafts)
        .where(drafts.c.id == draft_id, drafts.c.status.in_(from_statuses))
        .values(**values)
        .returning(drafts.c.id)
    )
    async with session_factory() as session, session.begin():
        changed = (await session.execute(statement)).scalar_one_or_none()
    return changed is not None


# --- summary ------------------------------------------------------------------------------


def _first_evidence(reasons: list[dict[str, Any]]) -> str | None:
    for reason in reasons:
        if reason.get("evidence"):
            return str(reason["evidence"])
    return None


async def summary(session_factory: SessionFactory, run_id: UUID) -> InboxSummary:
    """What one run did: counts per category and route, draft statuses, held messages."""
    async with session_factory() as session:
        triaged = (
            await session.execute(
                select(
                    triage_table.c.message_id,
                    triage_table.c.category,
                    triage_table.c.route,
                    triage_table.c.quarantined,
                    triage_table.c.injection_reasons,
                    messages.c.from_header,
                    messages.c.subject,
                )
                .join(messages, messages.c.message_id == triage_table.c.message_id)
                .where(triage_table.c.run_id == run_id)
                .order_by(messages.c.received_at, messages.c.message_id)
            )
        ).all()
        draft_statuses = (
            await session.execute(select(drafts.c.status).where(drafts.c.run_id == run_id))
        ).scalars()
        draft_counts = Counter(draft_statuses)
    return InboxSummary(
        counts=dict(Counter(row.category for row in triaged)),
        routes=dict(Counter(row.route for row in triaged)),
        drafts=dict(draft_counts),
        quarantined=[
            QuarantinedItem(
                message_id=row.message_id,
                sender=row.from_header,
                subject=row.subject,
                category=row.category,
                evidence=_first_evidence(row.injection_reasons),
            )
            for row in triaged
            if row.quarantined
        ],
    )
