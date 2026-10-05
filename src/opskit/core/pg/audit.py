"""The kit's Postgres audit log, implementing agent-core's hash-chained AuditLog protocol.

Records follow agent-core's schema 4 (2 and 3 for those already in the chain) and are sealed with
its compute_record_hash, so the chain verifies the same way agent-core's own logs do. Appends are
serialized with a transaction-scoped advisory lock, and append_in() lets a caller write its
change and the event recording it in one transaction. The database also refuses UPDATE, DELETE
and TRUNCATE on the table, and the app roles may only INSERT and SELECT.

`occurred_at` is the caller's if it gave one, else the database's clock; the insert trigger
refuses a time more than 24 hours back or 5 minutes ahead, and sets `recorded_at` itself. The hash
covers `occurred_at` only, never `recorded_at`, `db_role` or `db_login`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any
from uuid import uuid4

from aox_agent_core.audit import (
    GENESIS_HASH,
    OCCURRED_AT_MAX_FUTURE,
    OCCURRED_AT_MAX_PAST,
    AuditEvent,
    AuditHead,
    AuditRecord,
    UnsealedAuditRecord,
    compute_record_hash,
)
from aox_agent_core.context import RunContext
from aox_agent_core.errors import (
    AuditIntegrityError,
    AuditPayloadRejectedError,
    AuditTimeRejectedError,
)
from aox_agent_core.replay.scrub import PatternScrubber
from pydantic import JsonValue, ValidationError
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from opskit.core.secrets import GATEWAY_TOKEN_PATTERNS
from opskit.db.engine import SessionFactory
from opskit.db.tables import audit_log

# Any fixed 64-bit key; it names "the audit chain" for pg_advisory_xact_lock.
CHAIN_LOCK_KEY = 7_302_118_421
ITER_BATCH = 500
# The most events one append_many takes; agent-core's SQLAuditLog sets the same cap.
MAX_APPEND_BATCH = 1000

_scrubber = PatternScrubber(extra_patterns=GATEWAY_TOKEN_PATTERNS)


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
    return (await _append_checked(session, [checked_event(event)]))[0]


async def append_many_in(session: AsyncSession, events: Sequence[AuditEvent]) -> list[AuditRecord]:
    """Append every event, all or nothing, as consecutive records in order, under one lock.

    Every event is validated and scanned before anything is written, and an error names the index
    of the event it refused. At most MAX_APPEND_BATCH events; none writes nothing.
    """
    if not events:
        return []
    if len(events) > MAX_APPEND_BATCH:
        raise ValueError(f"append_many takes at most {MAX_APPEND_BATCH} events")
    checked: list[AuditEvent] = []
    for index, event in enumerate(events):
        try:
            checked.append(checked_event(event))
        except AuditPayloadRejectedError as error:
            raise AuditPayloadRejectedError(f"Event {index}: {error}") from error
    return await _append_checked(session, checked)


def _check_occurred_at(index: int, occurred_at: datetime | None, database_now: datetime) -> None:
    """Refuse a caller-supplied time too far from the database clock; the trigger checks again."""
    if occurred_at is None:
        return
    if occurred_at > database_now + OCCURRED_AT_MAX_FUTURE:
        raise AuditTimeRejectedError(
            f"Event {index}: occurred_at is more than {OCCURRED_AT_MAX_FUTURE} ahead of the "
            "database's clock."
        )
    if occurred_at < database_now - OCCURRED_AT_MAX_PAST:
        raise AuditTimeRejectedError(
            f"Event {index}: occurred_at is more than {OCCURRED_AT_MAX_PAST} behind the "
            "database's clock."
        )


async def _append_checked(session: AsyncSession, events: Sequence[AuditEvent]) -> list[AuditRecord]:
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CHAIN_LOCK_KEY})
    last = (
        await session.execute(
            select(audit_log.c.seq, audit_log.c.record_hash)
            .order_by(audit_log.c.seq.desc())
            .limit(1)
        )
    ).one_or_none()
    database_now: datetime = await session.scalar(select(func.clock_timestamp()))  # type: ignore[assignment]
    for index, event in enumerate(events):
        _check_occurred_at(index, event.occurred_at, database_now)

    seq = last.seq if last else 0
    prev_hash = last.record_hash if last else GENESIS_HASH
    records: list[AuditRecord] = []
    for event in events:
        seq += 1
        unsealed = UnsealedAuditRecord(
            seq=seq,
            event_id=uuid4(),
            occurred_at=event.occurred_at if event.occurred_at is not None else database_now,
            action=event.action,
            actor_id=event.actor_id,
            subject_id=event.subject_id,
            payload=event.payload,
            run_context=event.context,
            prev_hash=prev_hash,
        )
        record = AuditRecord(**unsealed.model_dump(), record_hash=compute_record_hash(unsealed))
        records.append(record)
        prev_hash = record.record_hash

    stored = (
        await session.execute(
            insert(audit_log)
            .values(
                [
                    {
                        "seq": record.seq,
                        "schema_version": record.schema_version,
                        "event_id": record.event_id,
                        "occurred_at": record.occurred_at,
                        "action": record.action,
                        "actor_id": record.actor_id,
                        "subject_id": record.subject_id,
                        "payload": _canonical(record.payload),
                        "run_context": (
                            _canonical(record.run_context.as_json()) if record.run_context else None
                        ),
                        "prev_hash": record.prev_hash,
                        "record_hash": record.record_hash,
                    }
                    for record in records
                ]
            )
            .returning(
                audit_log.c.seq,
                audit_log.c.db_role,
                audit_log.c.db_login,
                audit_log.c.recorded_at,
            )
        )
    ).all()
    # db_role, db_login and recorded_at are the database's own account of who wrote the row and
    # when; all are outside the hash.
    by_seq = {row.seq: row for row in stored}
    return [
        record.model_copy(
            update={
                "db_role": by_seq[record.seq].db_role,
                "db_login": by_seq[record.seq].db_login,
                "recorded_at": by_seq[record.seq].recorded_at,
            }
        )
        for record in records
    ]


async def append_once_in(session: AsyncSession, event: AuditEvent) -> AuditRecord | None:
    """Append unless a record with this action and subject already exists; None if it does.

    The check and the append share the chain's advisory lock, so two callers cannot both pass it.
    For a finding that would otherwise repeat on every read (an approval no reader can parse).
    """
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CHAIN_LOCK_KEY})
    seen = await session.execute(
        select(audit_log.c.seq)
        .where(audit_log.c.action == event.action, audit_log.c.subject_id == event.subject_id)
        .limit(1)
    )
    if seen.first() is not None:
        return None
    return await append_in(session, event)


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
        db_role=row.db_role,
        db_login=row.db_login,
        recorded_at=row.recorded_at,
    )


class PgAuditLog:
    """agent-core's AuditLog protocol on the kit's own Postgres table."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def append(self, event: AuditEvent) -> AuditRecord:
        async with self._session_factory.begin() as session:
            return await append_in(session, event)

    async def append_many(self, events: Sequence[AuditEvent]) -> list[AuditRecord]:
        async with self._session_factory.begin() as session:
            return await append_many_in(session, events)

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
        empty_head = expected_head is not None and expected_head.seq == 0
        if empty_head and expected_head.record_hash != GENESIS_HASH:  # type: ignore[union-attr]
            raise AuditIntegrityError("an empty expected head must carry the genesis hash")
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
