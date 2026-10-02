"""At-least-once delivery of approval decisions back to the waiting n8n execution."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update

from opskit.core.stub.audit_pg import append_in
from opskit.db.engine import SessionFactory
from opskit.db.tables import approvals, outbox

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 10
MAX_BACKOFF_SECONDS = 300
BATCH_SIZE = 10

# (stored resume url, payload) -> HTTP status code; raises on transport errors
type ResumeSender = Callable[[str, dict[str, Any]], Awaitable[int]]


def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(2**attempts, MAX_BACKOFF_SECONDS))


async def deliver_due(session_factory: SessionFactory, send: ResumeSender) -> int:
    """Try every due outbox row once. Returns how many were delivered."""
    claim = (
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
    delivered = 0
    async with session_factory.begin() as session:
        for row in (await session.execute(claim)).all():
            if await _attempt(session, row, send):
                delivered += 1
    return delivered


async def _attempt(session: Any, row: Any, send: ResumeSender) -> bool:
    attempts = row.attempts + 1
    try:
        status = await send(row.resume_url, row.payload)
        error = None if 200 <= status < 300 else f"n8n answered HTTP {status}"
    except Exception as exc:
        error = type(exc).__name__

    if error is None:
        await session.execute(
            update(outbox)
            .where(outbox.c.id == row.id)
            .values(attempts=attempts, delivered_at=func.now(), last_error=None)
        )
        await _audit(session, row, "approval.resumed", {"attempts": attempts})
        return True

    await session.execute(
        update(outbox)
        .where(outbox.c.id == row.id)
        .values(attempts=attempts, last_error=error, next_attempt_at=func.now() + backoff(attempts))
    )
    if attempts >= MAX_ATTEMPTS:
        await _audit(session, row, "resume.failed", {"attempts": attempts, "error": error})
    log.warning("resume of approval %s failed (attempt %d): %s", row.approval_id, attempts, error)
    return False


async def _audit(session: Any, row: Any, action: str, details: dict[str, Any]) -> None:
    await append_in(
        session,
        ctx=None,
        actor="system",
        action=action,
        subject_type="approval",
        subject_id=str(row.approval_id),
        details={"run_id": str(row.run_id), **details},
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
