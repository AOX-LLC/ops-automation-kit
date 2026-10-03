"""The kit's Postgres approval queue, implementing agent-core's ApprovalQueue protocol.

Resolution runs agent-core's RoleApproverPolicy inside a row lock, so a request is resolved
once, only by a human holding the required role who did not ask for it. Each change and the
audit record of it commit together; a decision also queues the n8n resume (the outbox) in
the same transaction. Kept in the kit, beyond the protocol: the stored payload (shown to the
approver), the resume URL, closing a timed-out request and a string-cursor page.

A pending request past its lifetime is reported as EXPIRED by every read, whether or not the
sweep has stored it yet; either way `closed_at` is its `expires_at`.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence
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
from aox_agent_core.approvals.types import MAX_DELEGATES
from aox_agent_core.audit import AuditEvent
from aox_agent_core.context import RunContext
from aox_agent_core.errors import (
    ApprovalAlreadyResolvedError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ApprovalNotGrantedError,
    ApprovalPayloadMismatchError,
    ConfigError,
    NotAuthorizedToResolveError,
    NotTheRequesterError,
)
from pydantic import JsonValue
from sqlalchemy import and_, func, insert, null, or_, select, update
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
    approvals.c.closed_at,
    approvals.c.reason,
    approvals.c.run_context,
    approvals.c.delegates,
]
EXPIRE_BATCH_MAX = 500
log = logging.getLogger(__name__)


def _request(row: Any) -> ApprovalRequest:
    data = {column.name: getattr(row, column.name) for column in REQUEST_COLUMNS}
    data["delegates"] = frozenset(data["delegates"] or ())
    return ApprovalRequest.model_validate(data)


def _readable(row: Any) -> ApprovalRequest | None:
    """A listing skips a row the model refuses (and says so) rather than failing the page."""
    try:
        return _request(row)
    except ValueError:
        log.error("approval %s is malformed and is left out of the listing", row.id)
        return None


def _judged(request: ApprovalRequest, now: datetime) -> ApprovalRequest:
    """What a reader sees: a pending request past its lifetime is expired, stored or not."""
    if request.status is ApprovalStatus.PENDING and request.is_expired(now):
        return request.model_copy(
            update={"status": ApprovalStatus.EXPIRED, "closed_at": request.expires_at}
        )
    return request


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


def _consume_refusal(
    request: ApprovalRequest,
    action: str,
    payload: Mapping[str, JsonValue],
    principal: Principal,
    now: datetime,
) -> tuple[Exception, str] | None:
    """Why this approval may not be used by this principal for this action now, with the reason
    recorded in the audit log; None if it may."""
    if principal.id != request.requested_by and principal.id not in request.delegates:
        return (
            NotTheRequesterError(
                f"approval {request.id} was made by {request.requested_by}; only they or a "
                "delegate it named may use it"
            ),
            DenialReason.NOT_REQUESTER.value,
        )
    if approval_payload_hash(action, payload) != request.payload_sha256:
        return (
            ApprovalPayloadMismatchError(
                f"approval {request.id} does not cover this action and payload"
            ),
            "payload_mismatch",
        )
    request = _judged(request, now)
    if request.status is ApprovalStatus.EXPIRED:
        return ApprovalExpiredError(f"approval {request.id} has expired"), "expired"
    if request.status in {ApprovalStatus.PENDING, ApprovalStatus.REJECTED}:
        return (
            ApprovalNotGrantedError(f"approval {request.id} is {request.status.value}"),
            "not_granted",
        )
    if request.status is not ApprovalStatus.APPROVED:
        return (
            ApprovalAlreadyResolvedError(f"approval {request.id} is {request.status.value}"),
            "not_open",
        )
    if request.is_expired(now):
        return ApprovalExpiredError(f"approval {request.id} has expired"), "expired"
    return None


def _cancel_refusal(
    request: ApprovalRequest, principal: Principal, now: datetime
) -> tuple[Exception, str] | None:
    """Why this principal may not withdraw this request now; only the requester may."""
    if principal.id != request.requested_by:
        return (
            NotTheRequesterError(
                f"approval {request.id} was made by {request.requested_by}; only they may cancel it"
            ),
            DenialReason.NOT_REQUESTER.value,
        )
    request = _judged(request, now)
    if request.status is ApprovalStatus.EXPIRED:
        return ApprovalExpiredError(f"approval {request.id} has expired"), "expired"
    if request.status is not ApprovalStatus.PENDING:
        return (
            ApprovalAlreadyResolvedError(f"approval {request.id} is {request.status.value}"),
            DenialReason.NOT_PENDING.value,
        )
    return None


class PgApprovalQueue:
    """Two database roles meet here. Everything but `resolve` runs on the requester role's
    sessions (the n8n-facing API); `resolve` alone uses the approver role's. The database
    refuses an approval from any other connection; nothing in this process stops a route
    from calling `resolve`, so only the approver page's decision route does."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        policy: RoleApproverPolicy,
        listed_actions: Collection[str],
        approver_session_factory: SessionFactory | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._approver_session_factory = approver_session_factory
        self._policy = policy
        self._listed_actions = sorted(listed_actions)

    async def submit(
        self,
        *,
        action: str,
        summary: str,
        payload: Mapping[str, JsonValue],
        requested_by: Principal,
        required_role: str,
        ttl_seconds: int,
        delegates: Collection[str] = (),
        context: RunContext | None = None,
        resume_url: str | None = None,
    ) -> ApprovalRequest:
        if not 0 < ttl_seconds <= TTL_SECONDS_MAX:
            raise ValueError(f"ttl_seconds must be between 1 and {TTL_SECONDS_MAX}")
        delegate_ids = sorted(set(delegates))
        if len(delegate_ids) > MAX_DELEGATES:
            raise ValueError(f"at most {MAX_DELEGATES} delegates")
        for delegate_id in delegate_ids:  # the same id rules as any principal
            Principal(id=delegate_id, kind=PrincipalKind.SERVICE)
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
                delegates=delegate_ids,
                resume_url=resume_url,
                run_context=context.as_json() if context is not None else null(),
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
                {
                    "action": action,
                    "expires_at": request.expires_at.isoformat(),
                    "payload_sha256": request.payload_sha256,
                    "delegates": list[JsonValue](delegate_ids),
                },
                context,
            )
        return request

    async def get(self, request_id: UUID) -> ApprovalRequest:
        query = select(*REQUEST_COLUMNS).where(approvals.c.id == request_id)
        async with self._session_factory() as session:
            row = (await session.execute(query)).one_or_none()
        if row is None:
            raise ApprovalNotFoundError(f"approval {request_id} not found")
        return _judged(_request(row), datetime.now(UTC))

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
                approvals.c.action.in_(self._listed_actions),
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
        return [request for row in rows if (request := _readable(row)) is not None]

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
        if self._approver_session_factory is None:
            raise ConfigError("this queue has no approver-role connection; it cannot resolve")
        now = datetime.now(UTC)
        async with self._approver_session_factory.begin() as session:
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
            verdict = self._policy.evaluate(principal, request, now=now)
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
            # Store the expiry if it is still pending and due; either way the refusal is audited.
            await self._store_expired(session, approvals.c.id == request.id, principal.id)
        await _audit(
            session,
            "approval.denied",
            principal.id,
            request.id,
            {"reason": reason.value if reason else "unknown"},
            None,
        )
        if reason is DenialReason.EXPIRED:
            return ApprovalExpiredError(f"approval {request.id} has expired")
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
            {"decision": decision.value, "payload_sha256": request.payload_sha256},
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
        """Use an approval for its one permitted run, checking it covers this exact action.

        Only the requester, or a delegate it named, may consume. The principal is checked first,
        then the payload hash, so a changed payload is refused as a mismatch whatever the
        status. Every refusal is audited (approval.consume_denied) and committed before the
        error is raised.
        """
        now = datetime.now(UTC)
        refusal: tuple[Exception, str] | None = None
        async with self._session_factory.begin() as session:
            row = await self._locked(session, request_id)
            refusal = _consume_refusal(_request(row), action, payload, principal, now)
            if refusal is not None:
                await _audit(
                    session,
                    "approval.consume_denied",
                    principal.id,
                    request_id,
                    {"reason": refusal[1]},
                    context,
                )
            else:
                row = (
                    await session.execute(
                        update(approvals)
                        .where(approvals.c.id == request_id)
                        .values(status=ApprovalStatus.CONSUMED.value, consumed_at=now)
                        .returning(*REQUEST_COLUMNS)
                    )
                ).one()
                await _audit(session, "approval.consumed", principal.id, request_id, {}, context)
        if refusal is not None:
            raise refusal[0]
        return _request(row)

    async def cancel(
        self,
        request_id: UUID,
        *,
        principal: Principal,
        reason: str | None = None,
        context: RunContext | None = None,
    ) -> ApprovalRequest:
        """The requester withdraws a pending request. A delegate may not."""
        now = datetime.now(UTC)
        refusal: tuple[Exception, str] | None = None
        async with self._session_factory.begin() as session:
            row = await self._locked(session, request_id)
            refusal = _cancel_refusal(_request(row), principal, now)
            if refusal is not None:
                await _audit(
                    session,
                    "approval.cancel_denied",
                    principal.id,
                    request_id,
                    {"reason": refusal[1]},
                    context,
                )
            else:
                row = (
                    await session.execute(
                        update(approvals)
                        .where(approvals.c.id == request_id)
                        .values(
                            status=ApprovalStatus.CANCELLED.value,
                            closed_at=func.statement_timestamp(),
                        )
                        .returning(*REQUEST_COLUMNS)
                    )
                ).one()
                await _audit(
                    session,
                    "approval.cancelled",
                    principal.id,
                    request_id,
                    {"cancel_reason": reason} if reason else {},
                    context,
                )
        if refusal is not None:
            raise refusal[0]
        return _request(row)

    async def _locked(self, session: AsyncSession, request_id: UUID) -> Any:
        row = (
            await session.execute(
                select(*REQUEST_COLUMNS).where(approvals.c.id == request_id).with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise ApprovalNotFoundError(f"approval {request_id} not found")
        return row

    async def close_pending(self, request_id: UUID, *, principal: Principal) -> ApprovalRequest:
        """End a request whose wait is over: expired if its lifetime has passed, else withdrawn.

        Any request that is no longer pending is returned as it is. This is the one call for a
        workflow that stopped waiting (n8n's Wait timed out) and for a loser of a race.
        """
        async with self._session_factory.begin() as session:
            await self._store_expired(session, approvals.c.id == request_id, principal.id)
        request = await self.get(request_id)
        if request.status is ApprovalStatus.PENDING:
            try:
                return await self.cancel(request_id, principal=principal, reason="wait ended")
            except (ApprovalAlreadyResolvedError, ApprovalExpiredError):
                return await self.get(request_id)
        return request

    async def expire_due(
        self, *, principal: Principal, now: datetime | None = None, limit: int = EXPIRE_BATCH_MAX
    ) -> int:
        """Store EXPIRED on pending requests past their lifetime; returns how many.

        Works in batches of `limit`. A request counts as due only if the database's clock
        agrees, so a skewed caller clock expires nothing early.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        total = 0
        while True:
            condition = approvals.c.id.in_(
                select(approvals.c.id)
                .where(
                    approvals.c.status == ApprovalStatus.PENDING.value,
                    approvals.c.expires_at <= (now or datetime.now(UTC)),
                    approvals.c.expires_at <= func.statement_timestamp(),
                )
                .order_by(approvals.c.expires_at, approvals.c.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            async with self._session_factory.begin() as session:
                batch = await self._store_expired(session, condition, principal.id)
            total += batch
            if batch < limit:
                return total

    async def _store_expired(self, session: AsyncSession, condition: Any, actor_id: str) -> int:
        """Store EXPIRED on pending rows matching `condition` that the database also finds due."""
        statement = (
            update(approvals)
            .where(
                condition,
                approvals.c.status == ApprovalStatus.PENDING.value,
                approvals.c.expires_at <= func.statement_timestamp(),
            )
            .values(status=ApprovalStatus.EXPIRED.value, closed_at=approvals.c.expires_at)
            .returning(approvals.c.id, approvals.c.run_context)
        )
        expired = (await session.execute(statement)).all()
        for request_id, run_context in expired:
            context = RunContext.model_validate(run_context) if run_context else None
            await _audit(session, "approval.expired", actor_id, request_id, {}, context)
        return len(expired)
