"""The kit's Postgres approval queue, implementing agent-core's ApprovalQueue protocol.

Resolution runs agent-core's RoleApproverPolicy inside a row lock, so a request is resolved
once, only by a human holding the required role who did not ask for it. Each change and the
audit record of it commit together; a decision also queues the n8n resume (the outbox) in
the same transaction. Kept in the kit, beyond the protocol: the stored payload (shown to the
approver), the resume URL, closing a timed-out request and a string-cursor page.

A pending request past its lifetime is reported as EXPIRED by every read, whether or not the
sweep has stored it yet; either way `closed_at` is its `expires_at`.

A row that cannot be parsed (the database bounds keep them out; this covers rows stored before
the bounds, and anything a bound cannot express) never crashes a reader or stops the sweep. A
listing skips it. A read, a decision, a use or a withdrawal of it raises ApprovalUnreadableError
and changes nothing, so a decision on it fails closed. Each such row is recorded once in the audit
log (`approval.unreadable`). The sweep closes it anyway, without its run context in the record.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator, Collection, Mapping, Sequence
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
from sqlalchemy import and_, func, insert, null, or_, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession

from opskit.core.errors import ApprovalUnreadableError
from opskit.core.pg.audit import append_in, append_once_in
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
# A request the sweep failed to close this many times in a row is left alone until restart; one
# failure may be a dropped connection or a deadlock, so a single one is not enough.
SWEEP_ATTEMPTS_MAX = 3
# Bounds the in-process note of which unreadable rows were already recorded.
RECORDED_MAX = 10_000
log = logging.getLogger(__name__)


def _request(row: Any) -> ApprovalRequest:
    data = {column.name: getattr(row, column.name) for column in REQUEST_COLUMNS}
    data["delegates"] = frozenset(data["delegates"] or ())
    return ApprovalRequest.model_validate(data)


# What a malformed row raises: the model refusing it, or a column of the wrong JSON type.
_UNPARSABLE = (ValueError, TypeError, KeyError)
UNREADABLE_ACTION = "approval.unreadable"
ACTOR_SYSTEM = "system"


class _UnreadableRow(Exception):
    """Raised inside a transaction; turned into ApprovalUnreadableError once it has rolled back."""

    def __init__(self, request_id: UUID) -> None:
        super().__init__(str(request_id))
        self.request_id = request_id


def _parsed(row: Any) -> ApprovalRequest:
    try:
        return _request(row)
    except _UNPARSABLE as exc:
        raise _UnreadableRow(row.id) from exc


def _readable(row: Any) -> ApprovalRequest | None:
    """A listing skips a row the model refuses (and says so) rather than failing the page."""
    try:
        return _request(row)
    except _UNPARSABLE:
        return None


def _covers(request: ApprovalRequest, payload: Any) -> bool:
    """Whether the stored payload is the one the approval's hash covers. A payload that is not
    an object, or that cannot be hashed, cannot be said to be: that row is unreadable."""
    if not isinstance(payload, dict):
        raise _UnreadableRow(request.id)
    try:
        return approval_payload_hash(request.action, payload) == request.payload_sha256
    except _UNPARSABLE as exc:
        raise _UnreadableRow(request.id) from exc


def _stored_context(run_context: Any) -> RunContext | None:
    """The run context of a stored row, or None if it cannot be parsed (or there is none)."""
    if not run_context:
        return None
    try:
        return RunContext.model_validate(run_context)
    except _UNPARSABLE:
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
        # Consecutive failures to close a request; at SWEEP_ATTEMPTS_MAX it is skipped until the
        # process restarts, so one bad row cannot hold up the others or be retried every minute.
        self._sweep_failures: dict[UUID, int] = {}
        # Unreadable rows already written to the audit log by this process, so a page view does
        # not take the audit chain's lock, or log, again for each one.
        self._recorded: set[UUID] = set()

    @contextlib.asynccontextmanager
    async def _reading(self, reader: str) -> AsyncIterator[None]:
        """Turn an unparsable row met inside the block into ApprovalUnreadableError, after the
        block's transaction has rolled back, recording it once in the audit log."""
        try:
            yield
        except _UnreadableRow as exc:
            await self._record_unreadable(exc.request_id, reader)
            raise ApprovalUnreadableError(exc.request_id) from exc

    async def _record_unreadable(self, request_id: UUID, reader: str) -> None:
        if request_id in self._recorded:
            return
        log.error("approval %s cannot be parsed; read by %s and left alone", request_id, reader)
        try:
            async with self._session_factory.begin() as session:
                await append_once_in(
                    session,
                    AuditEvent(
                        action=UNREADABLE_ACTION,
                        actor_id=ACTOR_SYSTEM,
                        subject_id=str(request_id),
                        payload={"subject_type": "approval", "reader": reader},
                    ),
                )
            if len(self._recorded) >= RECORDED_MAX:
                self._recorded.clear()
            self._recorded.add(request_id)
        except Exception:
            # Recording is best effort: failing to write the note must not turn a refused read
            # into a crash.
            log.exception("could not record unreadable approval %s", request_id)

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
        async with self._reading("get"):
            async with self._session_factory() as session:
                row = (await session.execute(query)).one_or_none()
            if row is None:
                raise ApprovalNotFoundError(f"approval {request_id} not found")
            return _judged(_parsed(row), datetime.now(UTC))

    async def payload_of(self, request_id: UUID) -> JsonObject:
        query = select(approvals.c.payload).where(approvals.c.id == request_id)
        async with self._reading("payload_of"):
            async with self._session_factory() as session:
                payload = (await session.execute(query)).scalar_one_or_none()
            if payload is None:
                raise ApprovalNotFoundError(f"approval {request_id} not found")
            if not isinstance(payload, dict):
                raise _UnreadableRow(request_id)
            return dict(payload)

    async def _pending_rows(
        self, principal: Principal, *, limit: int, after: UUID | None
    ) -> list[Any]:
        """Raw rows for the pending listing: unexpired, listed actions, a role this principal
        holds, not its own, oldest first."""
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
            return list((await session.execute(query)).all())

    async def list_pending(
        self, principal: Principal, *, limit: int = 100, after: UUID | None = None
    ) -> Sequence[ApprovalRequest]:
        """Pending, unexpired requests this principal may resolve, oldest first."""
        rows = await self._pending_rows(principal, limit=limit, after=after)
        return await self._readable_rows(rows)

    async def list_pending_page(
        self, principal: Principal, *, limit: int = 50, cursor: str | None = None
    ) -> Page[ApprovalRequest]:
        try:
            after = UUID(cursor) if cursor else None
        except ValueError:
            after = None
        rows = await self._pending_rows(principal, limit=limit + 1, after=after)
        # Paging counts the rows fetched, not the ones that parsed: an unreadable row must not
        # end the listing and hide every request after it.
        page = rows[:limit]
        next_cursor = str(page[-1].id) if len(rows) > limit else None
        return Page(items=await self._readable_rows(page), next_cursor=next_cursor)

    async def _readable_rows(self, rows: Sequence[Any]) -> list[ApprovalRequest]:
        """The rows that parse; each one that does not is recorded once and left out."""
        items: list[ApprovalRequest] = []
        for row in rows:
            request = _readable(row)
            if request is None:
                await self._record_unreadable(row.id, "listing")
            else:
                items.append(request)
        return items

    async def resolve(
        self,
        request_id: UUID,
        *,
        decision: Decision,
        principal: Principal,
        reason: str | None = None,
        context: RunContext | None = None,
    ) -> ApprovalRequest:
        async with self._reading("decision"):
            if self._approver_session_factory is None:
                raise ConfigError("this queue has no approver-role connection; it cannot resolve")
            now = datetime.now(UTC)
            async with self._approver_session_factory.begin() as session:
                row = (
                    await session.execute(
                        select(*REQUEST_COLUMNS, approvals.c.resume_url, approvals.c.payload)
                        .where(approvals.c.id == request_id)
                        .with_for_update()
                    )
                ).one_or_none()
                if row is None:
                    raise ApprovalNotFoundError(f"approval {request_id} not found")
                request = _parsed(row)
                verdict = self._policy.evaluate(principal, request, now=now)
                denial: Exception | None
                if verdict.allowed and not _covers(request, row.payload):
                    # What the approver is shown is not what the approval would cover: the
                    # requester role writes both columns, and the database cannot compare them.
                    await _audit(
                        session,
                        "approval.denied",
                        principal.id,
                        request.id,
                        {"reason": "payload_mismatch"},
                        None,
                    )
                    denial = NotAuthorizedToResolveError(
                        f"approval {request.id}: the payload shown does not match what it covers"
                    )
                elif not verdict.allowed:
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
        async with self._reading("consume"):
            now = datetime.now(UTC)
            refusal: tuple[Exception, str] | None = None
            async with self._session_factory.begin() as session:
                row = await self._locked(session, request_id)
                refusal = _consume_refusal(_parsed(row), action, payload, principal, now)
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
                    await _audit(
                        session, "approval.consumed", principal.id, request_id, {}, context
                    )
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
        async with self._reading("cancel"):
            now = datetime.now(UTC)
            refusal: tuple[Exception, str] | None = None
            async with self._session_factory.begin() as session:
                row = await self._locked(session, request_id)
                refusal = _cancel_refusal(_parsed(row), principal, now)
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

        Works in batches of `limit`, one transaction per request, so a request that cannot be
        closed is logged, and skipped after SWEEP_ATTEMPTS_MAX failures until the process
        restarts, instead of rolling back, and blocking, every other request in its batch. A
        request counts as due only if the database's clock agrees, so a skewed caller clock
        expires nothing early.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        total = 0
        failed_here: set[UUID] = set()  # a failure counts once per call, not once per pass
        while True:
            given_up = [*self._given_up_on(), *failed_here]
            query = (
                select(approvals.c.id)
                .where(
                    approvals.c.status == ApprovalStatus.PENDING.value,
                    approvals.c.expires_at <= (now or datetime.now(UTC)),
                    approvals.c.expires_at <= func.statement_timestamp(),
                    approvals.c.id.not_in(given_up) if given_up else true(),
                )
                .order_by(approvals.c.expires_at, approvals.c.id)
                .limit(limit)
            )
            async with self._session_factory() as session:
                due = list((await session.execute(query)).scalars())
            for request_id in due:
                closed = await self._expire_one(request_id, principal.id)
                if closed == 0 and request_id in self._sweep_failures:
                    failed_here.add(request_id)
                total += closed
            if len(due) < limit:
                return total

    def _given_up_on(self) -> list[UUID]:
        return [rid for rid, count in self._sweep_failures.items() if count >= SWEEP_ATTEMPTS_MAX]

    async def _expire_one(self, request_id: UUID, actor_id: str) -> int:
        try:
            async with self._session_factory.begin() as session:
                closed = await self._store_expired(session, approvals.c.id == request_id, actor_id)
        except Exception:
            count = self._sweep_failures.get(request_id, 0) + 1
            self._sweep_failures[request_id] = count
            log.exception(
                "approval %s could not be expired (attempt %d of %d)",
                request_id,
                count,
                SWEEP_ATTEMPTS_MAX,
            )
            return 0
        self._sweep_failures.pop(request_id, None)
        return closed

    async def _store_expired(self, session: AsyncSession, condition: Any, actor_id: str) -> int:
        """Store EXPIRED on pending rows matching `condition` that the database also finds due.

        A row whose run context cannot be parsed is still closed and audited, with the record
        saying so in place of the context."""
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
            context = _stored_context(run_context)
            note: dict[str, JsonValue] = (
                {"run_context": "unreadable"} if run_context and context is None else {}
            )
            await _audit(session, "approval.expired", actor_id, request_id, note, context)
        return len(expired)
