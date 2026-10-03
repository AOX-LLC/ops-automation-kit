"""The kit's Postgres approval queue, implementing agent-core's ApprovalQueue protocol.

Resolution runs agent-core's RoleApproverPolicy inside a row lock, so a request is resolved
once, only by a human holding the required role who did not ask for it. Each change and the
audit record of it commit together; a decision also queues the n8n resume (the outbox) in
the same transaction. Kept in the kit, beyond the protocol: the stored payload (shown to the
approver), the resume URL, the expiry sweep and a string-cursor page.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from aox_agent_core.approvals import (
    TTL_SECONDS_MAX,
    ApprovalRequest,
    ApprovalStatus,
    Decision,
    DenialReason,
    Principal,
    PrincipalKind,
    RoleApproverPolicy,
    approval_payload_hash,
)
from aox_agent_core.audit import AuditEvent
from aox_agent_core.context import RunContext
from aox_agent_core.errors import (
    ApprovalAlreadyResolvedError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ApprovalNotGrantedError,
    ApprovalPayloadMismatchError,
    NotAuthorizedToResolveError,
)
from pydantic import JsonValue
from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from opskit.core.pg.audit import append_in
from opskit.core.ports import JsonObject, Page
from opskit.db.engine import SessionFactory
from opskit.db.tables import approvals, outbox

# Every column agent-core's ApprovalRequest has; payload and resume_url stay in the kit.
REQUEST_COLUMNS = [
    approvals.c.id,
    approvals.c.action,
    approvals.c.summary,
    approvals.c.payload_sha256,
    approvals.c.requested_by,
    approvals.c.required_role,
    approvals.c.created_at,
    approvals.c.expires_at,
    approvals.c.status,
    approvals.c.decision,
    approvals.c.resolved_by,
    approvals.c.resolved_at,
    approvals.c.consumed_at,
    approvals.c.reason,
    approvals.c.run_context,
]
_policy = RoleApproverPolicy()


def _request(row: Any) -> ApprovalRequest:
    data = {column.name: getattr(row, column.name) for column in REQUEST_COLUMNS}
    return ApprovalRequest.model_validate(data)


def _run_id(context: RunContext | None) -> UUID | None:
    return UUID(context.run_id) if context is not None else None


async def _audit(
    session: AsyncSession,
    action: str,
    actor: str,
    request_id: UUID,
    payload: dict[str, JsonValue],
    context: RunContext | None,
) -> None:
    await append_in(
        session,
        AuditEvent(
            action=action,
            actor_id=actor,
            subject_id=str(request_id),
            payload={"subject_type": "approval", **payload},
            context=context,
        ),
    )


class PgApprovalQueue:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def submit(
        self,
        *,
        action: str,
        summary: str,
        payload: Mapping[str, JsonValue],
        requested_by: Principal,
        required_role: str,
        ttl_seconds: int,
        context: RunContext | None = None,
        resume_url: str | None = None,
    ) -> ApprovalRequest:
        if not 0 < ttl_seconds <= TTL_SECONDS_MAX:
            raise ValueError(f"ttl_seconds must be between 1 and {TTL_SECONDS_MAX}")
        now = datetime.now(UTC)
        statement = (
            insert(approvals)
            .values(
                run_id=_run_id(context),
                action=action,
                summary=summary,
                payload=dict(payload),
                payload_sha256=approval_payload_hash(action, payload),
                requested_by=requested_by.id,
                required_role=required_role,
                created_at=now,
                expires_at=now + timedelta(seconds=ttl_seconds),
                resume_url=resume_url,
                run_context=context.as_json() if context is not None else None,
            )
            .returning(*REQUEST_COLUMNS)
        )
        async with self._session_factory.begin() as session:
            request = _request((await session.execute(statement)).one())
            await _audit(
                session,
                "approval.requested",
                requested_by.id,
                request.id,
                {"action": action, "expires_at": request.expires_at.isoformat()},
                context,
            )
        return request

    async def get(self, request_id: UUID) -> ApprovalRequest:
        query = select(*REQUEST_COLUMNS).where(approvals.c.id == request_id)
        async with self._session_factory() as session:
            row = (await session.execute(query)).one_or_none()
        if row is None:
            raise ApprovalNotFoundError(f"approval {request_id} not found")
        return _request(row)

    async def payload_of(self, request_id: UUID) -> JsonObject:
        query = select(approvals.c.payload).where(approvals.c.id == request_id)
        async with self._session_factory() as session:
            payload = (await session.execute(query)).scalar_one_or_none()
        if payload is None:
            raise ApprovalNotFoundError(f"approval {request_id} not found")
        return dict(payload)

    async def list_pending(
        self, principal: Principal, *, limit: int = 100, after: UUID | None = None
    ) -> Sequence[ApprovalRequest]:
        """Pending, unexpired requests this principal may resolve, oldest first."""
        if principal.kind is not PrincipalKind.HUMAN or not principal.roles:
            return []
        query = (
            select(*REQUEST_COLUMNS)
            .where(
                approvals.c.status == ApprovalStatus.PENDING.value,
                approvals.c.expires_at > datetime.now(UTC),
                approvals.c.required_role.in_(sorted(principal.roles)),
                approvals.c.requested_by != principal.id,
            )
            .order_by(approvals.c.created_at, approvals.c.id)
            .limit(limit)
        )
        if after is not None:
            anchor = select(approvals.c.created_at).where(approvals.c.id == after)
            query = query.where(
                or_(
                    approvals.c.created_at > anchor.scalar_subquery(),
                    and_(
                        approvals.c.created_at == anchor.scalar_subquery(),
                        approvals.c.id > after,
                    ),
                )
            )
        async with self._session_factory() as session:
            rows = (await session.execute(query)).all()
        return [_request(row) for row in rows]

    async def list_pending_page(
        self, principal: Principal, *, limit: int = 50, cursor: str | None = None
    ) -> Page[ApprovalRequest]:
        try:
            after = UUID(cursor) if cursor else None
        except ValueError:
            after = None
        items = list(await self.list_pending(principal, limit=limit + 1, after=after))
        next_cursor = str(items[limit - 1].id) if len(items) > limit else None
        return Page(items=items[:limit], next_cursor=next_cursor)

    async def resolve(
        self,
        request_id: UUID,
        *,
        decision: Decision,
        principal: Principal,
        reason: str | None = None,
        context: RunContext | None = None,
    ) -> ApprovalRequest:
        now = datetime.now(UTC)
        async with self._session_factory.begin() as session:
            row = (
                await session.execute(
                    select(*REQUEST_COLUMNS, approvals.c.resume_url)
                    .where(approvals.c.id == request_id)
                    .with_for_update()
                )
            ).one_or_none()
            if row is None:
                raise ApprovalNotFoundError(f"approval {request_id} not found")
            request = _request(row)
            verdict = _policy.evaluate(principal, request, now=now)
            if not verdict.allowed:
                denial = await self._record_denial(session, request, principal, verdict.reason)
            else:
                denial = None
                request = await self._apply_decision(
                    session, request, row.resume_url, decision, principal, reason, context, now
                )
        if denial is not None:
            raise denial
        return request

    async def _record_denial(
        self,
        session: AsyncSession,
        request: ApprovalRequest,
        principal: Principal,
        reason: DenialReason | None,
    ) -> Exception:
        """Write the refusal (and an expiry, if that is why) and return the error to raise."""
        if reason is DenialReason.EXPIRED:
            await self._expire_rows(session, approvals.c.id == request.id)
            return ApprovalExpiredError(f"approval {request.id} has expired")
        await _audit(
            session,
            "approval.denied",
            principal.id,
            request.id,
            {"reason": reason.value if reason else "unknown"},
            None,
        )
        if reason is DenialReason.NOT_PENDING:
            return ApprovalAlreadyResolvedError(
                f"approval {request.id} is already {request.status.value}"
            )
        return NotAuthorizedToResolveError(f"{principal.id} may not resolve approval {request.id}")

    async def _apply_decision(
        self,
        session: AsyncSession,
        request: ApprovalRequest,
        resume_url: str | None,
        decision: Decision,
        principal: Principal,
        reason: str | None,
        context: RunContext | None,
        now: datetime,
    ) -> ApprovalRequest:
        status = (
            ApprovalStatus.APPROVED if decision is Decision.APPROVE else ApprovalStatus.REJECTED
        )
        row = (
            await session.execute(
                update(approvals)
                .where(approvals.c.id == request.id)
                .values(
                    status=status.value,
                    decision=decision.value,
                    resolved_by=principal.id,
                    resolved_at=now,
                    reason=reason,
                )
                .returning(*REQUEST_COLUMNS)
            )
        ).one()
        resolved = _request(row)
        await _audit(
            session,
            "approval.decided",
            principal.id,
            request.id,
            {"decision": decision.value},
            context or request.run_context,
        )
        if resume_url:
            await session.execute(
                insert(outbox).values(
                    approval_id=request.id,
                    payload={"approval_id": str(request.id), "decision": decision.value},
                )
            )
        return resolved

    async def consume(
        self,
        request_id: UUID,
        *,
        action: str,
        payload: Mapping[str, JsonValue],
        principal: Principal,
        context: RunContext | None = None,
    ) -> ApprovalRequest:
        """Use an approval for its one permitted run, checking it covers this exact action."""
        now = datetime.now(UTC)
        async with self._session_factory.begin() as session:
            row = (
                await session.execute(
                    select(*REQUEST_COLUMNS).where(approvals.c.id == request_id).with_for_update()
                )
            ).one_or_none()
            if row is None:
                raise ApprovalNotFoundError(f"approval {request_id} not found")
            request = _request(row)
            if request.status is ApprovalStatus.CONSUMED:
                raise ApprovalAlreadyResolvedError(f"approval {request_id} was already used")
            if request.status is not ApprovalStatus.APPROVED:
                raise ApprovalNotGrantedError(f"approval {request_id} is {request.status.value}")
            if request.is_expired(now):
                raise ApprovalExpiredError(f"approval {request_id} has expired")
            if approval_payload_hash(action, payload) != request.payload_sha256:
                raise ApprovalPayloadMismatchError(
                    f"approval {request_id} does not cover this action and payload"
                )
            row = (
                await session.execute(
                    update(approvals)
                    .where(approvals.c.id == request_id)
                    .values(status=ApprovalStatus.CONSUMED.value, consumed_at=now)
                    .returning(*REQUEST_COLUMNS)
                )
            ).one()
            await _audit(session, "approval.consumed", principal.id, request_id, {}, context)
        return _request(row)

    async def expire(self, request_id: UUID) -> ApprovalRequest:
        """Expire one request if it is still pending; any other state is left as it is."""
        async with self._session_factory.begin() as session:
            await self._expire_rows(session, approvals.c.id == request_id)
        return await self.get(request_id)

    async def expire_due(self, *, now: datetime) -> int:
        async with self._session_factory.begin() as session:
            return await self._expire_rows(session, approvals.c.expires_at <= now)

    async def _expire_rows(self, session: AsyncSession, condition: Any) -> int:
        statement = (
            update(approvals)
            .where(condition, approvals.c.status == ApprovalStatus.PENDING.value)
            .values(status=ApprovalStatus.EXPIRED.value)
            .returning(approvals.c.id, approvals.c.run_context)
        )
        expired = (await session.execute(statement)).all()
        for request_id, run_context in expired:
            context = RunContext.model_validate(run_context) if run_context else None
            await _audit(session, "approval.expired", "system", request_id, {}, context)
        return len(expired)
