"""At-least-once delivery of approval decisions back to the waiting n8n execution.

Each pass claims due rows in one short transaction by pushing their next attempt out by a
lease, sends with no transaction open, then records each result in its own short
transaction. A worker that dies mid-send leaves the row to be retried once the lease runs out.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from aox_agent_core.audit import AuditEvent
from sqlalchemy import func, select, update

from opskit.core.pg.audit import append_in
from opskit.db.engine import SessionFactory
from opskit.db.tables import approvals, outbox

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 10
MAX_BACKOFF_SECONDS = 300
BATCH_SIZE = 10
# Each send gets a hard deadline, and a whole batch (BATCH_SIZE x SEND_DEADLINE_S = 100 s)
# fits inside the lease, so a live worker's claim never lapses while it is still sending.
SEND_DEADLINE_S = 10.0
CLAIM_LEASE = timedelta(seconds=120)

# (stored resume url, payload) -> HTTP status code; raises on transport errors
type ResumeSender = Callable[[str, dict[str, Any]], Awaitable[int]]


@dataclass(frozen=True, slots=True)
class Claim:
    outbox_id: int
    approval_id: UUID
    run_id: UUID | None
    resume_url: str
    payload: dict[str, Any]
    attempts: int


def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(2**attempts, MAX_BACKOFF_SECONDS))


async def claim_due(session_factory: SessionFactory) -> list[Claim]:
    """Lease up to BATCH_SIZE due rows and commit, so no lock outlives this call."""
    due = (
        select(
            outbox.c.id,
            outbox.c.approval_id,
            outbox.c.payload,
            outbox.c.attempts,
            approvals.c.resume_url,
            approvals.c.run_id,
        )
        .join(approvals, approvals.c.id == outbox.c.approval_id)
        .where(
            outbox.c.delivered_at.is_(None),
            outbox.c.attempts < MAX_ATTEMPTS,
            outbox.c.next_attempt_at <= func.now(),
        )
        .order_by(outbox.c.next_attempt_at)
        .limit(BATCH_SIZE)
        .with_for_update(of=outbox, skip_locked=True)
    )
    async with session_factory.begin() as session:
        rows = (await session.execute(due)).all()
        if rows:
            await session.execute(
                update(outbox)
                .where(outbox.c.id.in_([row.id for row in rows]))
                .values(next_attempt_at=func.now() + CLAIM_LEASE)
            )
    return [
        Claim(
            outbox_id=row.id,
            approval_id=row.approval_id,
            run_id=row.run_id,
            resume_url=row.resume_url,
            payload=row.payload,
            attempts=row.attempts,
        )
        for row in rows
    ]


async def _send(claim: Claim, send: ResumeSender) -> str | None:
    """Deliver one decision. Returns None on success, else a short error description."""
    try:
        async with asyncio.timeout(SEND_DEADLINE_S):
            status = await send(claim.resume_url, claim.payload)
    except Exception as exc:
        return type(exc).__name__
    return None if 200 <= status < 300 else f"n8n answered HTTP {status}"


async def record_result(session_factory: SessionFactory, claim: Claim, error: str | None) -> None:
    attempts = claim.attempts + 1
    async with session_factory.begin() as session:
        if error is None:
            await session.execute(
                update(outbox)
                .where(outbox.c.id == claim.outbox_id)
                .values(attempts=attempts, delivered_at=func.now(), last_error=None)
            )
            await _audit(session, claim, "approval.resumed", {"attempts": attempts})
            return
        await session.execute(
            update(outbox)
            .where(outbox.c.id == claim.outbox_id)
            .values(
                attempts=attempts,
                last_error=error,
                next_attempt_at=func.now() + backoff(attempts),
            )
        )
        if attempts >= MAX_ATTEMPTS:
            await _audit(session, claim, "resume.failed", {"attempts": attempts, "error": error})
    log.warning("resume of approval %s failed (attempt %d): %s", claim.approval_id, attempts, error)


async def deliver_due(session_factory: SessionFactory, send: ResumeSender) -> int:
    """Try every due outbox row once. Returns how many were delivered."""
    delivered = 0
    for claim in await claim_due(session_factory):
        error = await _send(claim, send)  # no transaction is open here
        await record_result(session_factory, claim, error)
        delivered += error is None
    return delivered


async def _audit(session: Any, claim: Claim, action: str, details: dict[str, Any]) -> None:
    await append_in(
        session,
        AuditEvent(
            action=action,
            actor_id="system",
            subject_id=str(claim.approval_id),
            payload={
                "subject_type": "approval",
                **({"run_id": str(claim.run_id)} if claim.run_id else {}),
                **details,
            },
        ),
    )


async def run_forever(
    session_factory: SessionFactory, send: ResumeSender, *, interval_s: float = 1.0
) -> None:
    while True:
        try:
            await deliver_due(session_factory, send)
        except Exception:
            log.exception("resume outbox pass failed")
        await asyncio.sleep(interval_s)
