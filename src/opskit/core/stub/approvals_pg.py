"""Postgres approval queue. A decision, its audit row and the n8n resume job commit together."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from opskit.core.errors import ApprovalExpired, ApprovalNotPending, NotFound
from opskit.core.ports import (
    Approval,
    ApprovalStatus,
    Decision,
    JsonObject,
    Page,
    RunContext,
)
from opskit.core.stub.audit_pg import append_in
from opskit.db.engine import SessionFactory
from opskit.db.tables import approvals, outbox

# Every column except resume_url: the signed n8n URL never leaves the adapter.
PUBLIC_COLUMNS = [column for column in approvals.c if column.name != "resume_url"]


def _approval(row: Any) -> Approval:
    return Approval(
        id=row.id,
        run_id=row.run_id,
        kind=row.kind,
        subject=row.subject,
        edited_subject=row.edited_subject,
        status=ApprovalStatus(row.status),
        requested_at=row.requested_at,
        expires_at=row.expires_at,
        decided_at=row.decided_at,
        decided_by=row.decided_by,
        decision_note=row.decision_note,
    )


def _encode_cursor(requested_at: datetime, approval_id: UUID) -> str:
    raw = json.dumps([requested_at.isoformat(), str(approval_id)]).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    requested_at, approval_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    return datetime.fromisoformat(requested_at), UUID(approval_id)


class PgApprovalQueue:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def request(
        self,
        *,
        ctx: RunContext,
        kind: str,
        subject: JsonObject,
        resume_url: str,
        expires_in: timedelta,
    ) -> Approval:
        statement = (
            insert(approvals)
            .values(
                run_id=ctx.run_id,
                kind=kind,
                subject=subject,
                resume_url=resume_url,
                expires_at=func.now() + expires_in,
            )
            .returning(*PUBLIC_COLUMNS)
        )
        async with self._session_factory.begin() as session:
            approval = _approval((await session.execute(statement)).one())
            await append_in(
                session,
                ctx=ctx,
                actor="n8n",
                action="approval.requested",
                subject_type="approval",
                subject_id=str(approval.id),
                details={"kind": kind, "expires_at": approval.expires_at.isoformat()},
            )
        return approval

    async def decide(
        self,
        approval_id: UUID,
        *,
        decision: Decision,
        actor: str,
        note: str | None = None,
        edited_subject: JsonObject | None = None,
    ) -> Approval:
        statement = (
            update(approvals)
            .where(
                approvals.c.id == approval_id,
                approvals.c.status == ApprovalStatus.PENDING.value,
                approvals.c.expires_at > func.now(),
            )
            .values(
                status=decision.value,
                decided_at=func.now(),
                decided_by=actor,
                decision_note=note,
                edited_subject=edited_subject,
            )
            .returning(*PUBLIC_COLUMNS)
        )
        async with self._session_factory.begin() as session:
            row = (await session.execute(statement)).one_or_none()
            if row is None:
                await self._explain_refusal(session, approval_id)
            approval = _approval(row)
            await append_in(
                session,
                ctx=None,
                actor=actor,
                action="approval.decided",
                subject_type="approval",
                subject_id=str(approval_id),
                details={"decision": decision.value, "run_id": str(approval.run_id)},
            )
            await session.execute(
                insert(outbox).values(
                    approval_id=approval_id,
                    payload={
                        "approval_id": str(approval_id),
                        "decision": decision.value,
                        "edited_subject": edited_subject,
                    },
                )
            )
        return approval

    async def _explain_refusal(self, session: AsyncSession, approval_id: UUID) -> None:
        """Raise the specific reason a conditional decision matched no row."""
        query = select(approvals.c.status, approvals.c.expires_at <= func.now()).where(
            approvals.c.id == approval_id
        )
        found = (await session.execute(query)).one_or_none()
        if found is None:
            raise NotFound("approval", approval_id)
        status, is_past_expiry = found
        if status == ApprovalStatus.PENDING.value and is_past_expiry:
            raise ApprovalExpired(approval_id)
        raise ApprovalNotPending(approval_id, status)

    async def get(self, approval_id: UUID) -> Approval:
        query = select(*PUBLIC_COLUMNS).where(approvals.c.id == approval_id)
        async with self._session_factory() as session:
            row = (await session.execute(query)).one_or_none()
        if row is None:
            raise NotFound("approval", approval_id)
        return _approval(row)

    async def list_pending(self, *, limit: int = 50, cursor: str | None = None) -> Page[Approval]:
        query = (
            select(*PUBLIC_COLUMNS)
            .where(approvals.c.status == ApprovalStatus.PENDING.value)
            .order_by(approvals.c.requested_at, approvals.c.id)
            .limit(limit + 1)
        )
        if cursor:
            after_time, after_id = _decode_cursor(cursor)
            query = query.where(
                or_(
                    approvals.c.requested_at > after_time,
                    and_(approvals.c.requested_at == after_time, approvals.c.id > after_id),
                )
            )
        async with self._session_factory() as session:
            rows = (await session.execute(query)).all()
        items = [_approval(row) for row in rows[:limit]]
        next_cursor = (
            _encode_cursor(items[-1].requested_at, items[-1].id) if len(rows) > limit else None
        )
        return Page(items=items, next_cursor=next_cursor)

    async def expire(self, approval_id: UUID) -> Approval:
        """Expire one approval if it is still pending; idempotent for any other state."""
        async with self._session_factory.begin() as session:
            await self._expire_where(session, approvals.c.id == approval_id)
        return await self.get(approval_id)

    async def expire_due(self, *, now: datetime) -> int:
        async with self._session_factory.begin() as session:
            return await self._expire_where(session, approvals.c.expires_at <= now)

    async def _expire_where(self, session: AsyncSession, condition: Any) -> int:
        statement = (
            update(approvals)
            .where(condition, approvals.c.status == ApprovalStatus.PENDING.value)
            .values(status=ApprovalStatus.EXPIRED.value, decided_at=func.now(), decided_by="system")
            .returning(approvals.c.id, approvals.c.run_id)
        )
        expired = (await session.execute(statement)).all()
        for approval_id, run_id in expired:
            await append_in(
                session,
                ctx=None,
                actor="system",
                action="approval.expired",
                subject_type="approval",
                subject_id=str(approval_id),
                details={"run_id": str(run_id)},
            )
        return len(expired)
