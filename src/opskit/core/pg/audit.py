"""The kit's Postgres audit log, implementing agent-core's hash-chained AuditLog protocol.

Records follow agent-core's schema 2 and are sealed with its compute_record_hash, so the
chain verifies the same way agent-core's own logs do. Appends are serialized with a
transaction-scoped advisory lock, and append_in() lets a caller write its change and the
event recording it in one transaction. The database also refuses UPDATE, DELETE and
TRUNCATE on the table, and the app role may only INSERT and SELECT.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from aox_agent_core.audit import (
    GENESIS_HASH,
    AuditEvent,
    AuditHead,
    AuditRecord,
    UnsealedAuditRecord,
    compute_record_hash,
)
from aox_agent_core.context import RunContext
from aox_agent_core.errors import AuditIntegrityError, AuditPayloadRejectedError
from aox_agent_core.replay.scrub import PatternScrubber
from pydantic import JsonValue, ValidationError
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from opskit.db.engine import SessionFactory
from opskit.db.tables import audit_log

# Any fixed 64-bit key; it names "the audit chain" for pg_advisory_xact_lock.
CHAIN_LOCK_KEY = 7_302_118_421
ITER_BATCH = 500

_scrubber = PatternScrubber()


def checked_event(event: AuditEvent) -> AuditEvent:
    """Re-validate an event and refuse one whose payload or context holds a secret."""
    try:
        revalidated = AuditEvent.model_validate(event.model_dump())
    except ValidationError as error:
        raise AuditPayloadRejectedError(
            "The event was changed after it was built and is no longer valid."
        ) from error
    scanned: dict[str, JsonValue] = {"payload": revalidated.payload}
    if revalidated.context is not None:
        scanned["context"] = revalidated.context.as_json()
    findings = _scrubber.find_secrets(scanned)
    if findings:
        located = ", ".join(f"{finding.rule} at {finding.path}" for finding in findings)
        raise AuditPayloadRejectedError(f"The audit event contains {located}.")
    return revalidated


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


async def append_in(session: AsyncSession, event: AuditEvent) -> AuditRecord:
    """Append within the caller's transaction, so the event commits with its change."""
    checked = checked_event(event)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CHAIN_LOCK_KEY})
    last = (
        await session.execute(
            select(audit_log.c.seq, audit_log.c.record_hash)
            .order_by(audit_log.c.seq.desc())
            .limit(1)
        )
    ).one_or_none()
    unsealed = UnsealedAuditRecord(
        seq=(last.seq + 1) if last else 1,
        event_id=uuid4(),
        occurred_at=datetime.now(UTC),
        action=checked.action,
        actor_id=checked.actor_id,
        subject_id=checked.subject_id,
        payload=checked.payload,
        run_context=checked.context,
        prev_hash=last.record_hash if last else GENESIS_HASH,
    )
    record = AuditRecord(**unsealed.model_dump(), record_hash=compute_record_hash(unsealed))
    await session.execute(
        insert(audit_log).values(
            seq=record.seq,
            schema_version=record.schema_version,
            event_id=record.event_id,
            occurred_at=record.occurred_at,
            action=record.action,
            actor_id=record.actor_id,
            subject_id=record.subject_id,
            payload=_canonical(record.payload),
            run_context=(_canonical(record.run_context.as_json()) if record.run_context else None),
            prev_hash=record.prev_hash,
            record_hash=record.record_hash,
        )
    )
    return record


def _record(row: Any) -> AuditRecord:
    return AuditRecord(
        schema_version=row.schema_version,
        seq=row.seq,
        event_id=row.event_id,
        occurred_at=row.occurred_at,
        action=row.action,
        actor_id=row.actor_id,
        subject_id=row.subject_id,
        payload=json.loads(row.payload),
        run_context=(
            RunContext.model_validate(json.loads(row.run_context)) if row.run_context else None
        ),
        prev_hash=row.prev_hash,
        record_hash=row.record_hash,
    )


class PgAuditLog:
    """agent-core's AuditLog protocol on the kit's own Postgres table."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def append(self, event: AuditEvent) -> AuditRecord:
        async with self._session_factory.begin() as session:
            return await append_in(session, event)

    async def iter_records(self, *, after_seq: int = 0) -> AsyncIterator[AuditRecord]:
        cursor = after_seq
        while True:
            async with self._session_factory() as session:
                rows = (
                    await session.execute(
                        select(audit_log)
                        .where(audit_log.c.seq > cursor)
                        .order_by(audit_log.c.seq)
                        .limit(ITER_BATCH)
                    )
                ).all()
            if not rows:
                return
            for row in rows:
                yield _record(row)
            cursor = rows[-1].seq

    async def head(self) -> AuditHead:
        async with self._session_factory() as session:
            last = (
                await session.execute(
                    select(audit_log.c.seq, audit_log.c.record_hash)
                    .order_by(audit_log.c.seq.desc())
                    .limit(1)
                )
            ).one_or_none()
        if last is None:
            return AuditHead(seq=0, record_hash=GENESIS_HASH)
        return AuditHead(seq=last.seq, record_hash=last.record_hash)

    async def verify(self, *, expected_head: AuditHead | None = None) -> AuditHead:
        previous = AuditHead(seq=0, record_hash=GENESIS_HASH)
        seen_expected = expected_head is None or expected_head.seq == 0
        async for record in self.iter_records():
            if record.seq != previous.seq + 1:
                raise AuditIntegrityError(f"audit seq jumps from {previous.seq} to {record.seq}")
            if record.prev_hash != previous.record_hash:
                raise AuditIntegrityError(f"audit record {record.seq} does not link to its parent")
            if compute_record_hash(record) != record.record_hash:
                raise AuditIntegrityError(f"audit record {record.seq} does not match its hash")
            if expected_head is not None and record.seq == expected_head.seq:
                if record.record_hash != expected_head.record_hash:
                    raise AuditIntegrityError(
                        f"audit record {record.seq} differs from the expected head"
                    )
                seen_expected = True
            previous = AuditHead(seq=record.seq, record_hash=record.record_hash)
        if not seen_expected:
            raise AuditIntegrityError("the audit log is shorter than the expected head")
        return previous
