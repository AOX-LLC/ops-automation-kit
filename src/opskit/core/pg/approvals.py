"""The kit's Postgres approval queue, implementing agent-core's ApprovalQueue protocol.

Resolution runs agent-core's RoleApproverPolicy inside a row lock, so a request is resolved
once, only by a human holding the required role who did not ask for it. Each change and the
audit record of it commit together; a decision also queues the n8n resume (the outbox) in
the same transaction. Kept in the kit, beyond the protocol: the stored payload (shown to the
approver), the resume URL, closing a timed-out request and a string-cursor page.

A pending or approved request past its lifetime is reported as EXPIRED by every read, whether or
not the sweep has stored it yet; either way `closed_at` is its `expires_at`. An approval that lapses
unused keeps its decision: EXPIRED may carry `approve`.

Submit is idempotent: at most one request is open (pending, or approved and not yet used) per
requester, action and payload hash, which the database's unique index enforces. An exact repeat
returns the open request and writes no audit record; a repeat on other terms is an
ApprovalConflictError. The resume URL is one of the terms: it is the only way a decision reaches
the waiting n8n execution, so a retried execution (which has a new one) must not be handed an
approval that would resume the old one.

The stored payload is the one the hash covers, checked on every read of it: `get` and `payload_of`
raise ApprovalIntegrityError, the listing omits the request, and a decision is refused. Withdrawing,
using and expiring never check, so a requester can always close a request. `purge_payloads`
removes the payload of a finished request after the retention floor.

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
from dataclasses import dataclass
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
    ApprovalConflictError,
    ApprovalExpiredError,
    ApprovalIntegrityError,
    ApprovalNotFoundError,
    ApprovalNotGrantedError,
    ApprovalPayloadMismatchError,
    ConfigError,
    NotAuthorizedToResolveError,
    NotTheRequesterError,
)
from pydantic import JsonValue
from sqlalchemy import and_, case, extract, func, insert, null, or_, select, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from opskit.core.errors import (
    ApprovalPayloadPurgedError,
    ApprovalUnreadableError,
    NotAuthorizedToPurgeError,
)
from opskit.core.pg.audit import append_in, append_many_in, append_once_in
from opskit.core.ports import PURGE_ROLES, JsonObject, Page
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
    approvals.c.payload_purged_at,
]
EXPIRE_BATCH_MAX = 500
# The shortest age a finished request's payload may be purged at; the approvals guard enforces the
# same number (core_0012), so a smaller value is refused here and by the database.
PAYLOAD_RETENTION_FLOOR = timedelta(hours=24)
PURGE_BATCH_MAX = 500
# The unique index that allows one open request per requester, action and payload hash.
OPEN_REQUEST_INDEX = "approvals_one_open"
OPEN_STATUSES = (ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value)
# A racing identical submit can take the key between our look and our insert; look again.
SUBMIT_ATTEMPTS = 3
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


_MISMATCH = "the payload stored with approval {id} is not the one its hash covers"


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


def _stored_payload_is_covered(request: ApprovalRequest, payload: Any) -> bool:
    try:
        return _covers(request, payload)
    except _UnreadableRow:
        return False


def _stored_context(run_context: Any) -> RunContext | None:
    """The run context of a stored row, or None if it cannot be parsed (or there is none)."""
    if not run_context:
        return None
    try:
        return RunContext.model_validate(run_context)
    except _UNPARSABLE:
        return None


def _judged(request: ApprovalRequest, now: datetime) -> ApprovalRequest:
    """What a reader sees: an open request past its lifetime is expired, stored or not. An
    approval that lapsed unused keeps its decision."""
    if request.status in {ApprovalStatus.PENDING, ApprovalStatus.APPROVED} and request.is_expired(
        now
    ):
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


@dataclass(frozen=True, slots=True)
class _Terms:
    """What one submit asks for, validated."""

    action: str
    summary: str
    payload: Mapping[str, JsonValue]
    payload_sha256: str
    requested_by: Principal
    required_role: str
    ttl_seconds: int
    delegates: list[str]
    context: RunContext | None
    resume_url: str | None


def _differences(
    existing: ApprovalRequest, stored_payload: Any, stored_resume_url: str | None, terms: _Terms
) -> tuple[str, ...]:
    """What an open request has that a repeat submit does not ask for, sorted."""
    differs = []
    if existing.summary != terms.summary:
        differs.append("summary")
    if existing.required_role != terms.required_role:
        differs.append("required_role")
    if existing.delegates != frozenset(terms.delegates):
        differs.append("delegates")
    if (stored_resume_url or None) != (terms.resume_url or None):
        differs.append("resume_url")
    lifetime = (existing.expires_at - existing.created_at).total_seconds()
    if abs(lifetime - terms.ttl_seconds) > 0.001:
        differs.append("lifetime")
    try:
        covered = _covers(existing, stored_payload)
    except _UnreadableRow:
        covered = False
    if not covered:
        differs.append("payload")
    return tuple(sorted(differs))


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
        include_payload: bool = False,
    ) -> ApprovalRequest:
        """Queue a request, or return the open one this repeats.

        The payload is always stored, because the approver must see what the hash binds;
        `include_payload` only decides whether the returned request carries it. An exact repeat of
        an open request (same summary, role, lifetime, delegates and resume URL) returns it,
        unchanged and unaudited; any difference raises ApprovalConflictError. `context` is not
        compared: the first submit's stays.
        """
        if not 0 < ttl_seconds <= TTL_SECONDS_MAX:
            raise ValueError(f"ttl_seconds must be between 1 and {TTL_SECONDS_MAX}")
        delegate_ids = sorted(set(delegates))
        if len(delegate_ids) > MAX_DELEGATES:
            raise ValueError(f"at most {MAX_DELEGATES} delegates")
        for delegate_id in delegate_ids:  # the same id rules as any principal
            Principal(id=delegate_id, kind=PrincipalKind.SERVICE)
        terms = _Terms(
            action=action,
            summary=summary,
            payload=payload,
            payload_sha256=approval_payload_hash(action, payload),
            requested_by=requested_by,
            required_role=required_role,
            ttl_seconds=ttl_seconds,
            delegates=delegate_ids,
            context=context,
            resume_url=resume_url,
        )
        for attempt in range(1, SUBMIT_ATTEMPTS + 1):
            try:
                async with self._reading("submit"):
                    request = await self._submit_once(terms)
            except IntegrityError as error:
                if OPEN_REQUEST_INDEX not in str(error) or attempt == SUBMIT_ATTEMPTS:
                    raise
                continue  # an identical submit won the race; the next look finds it
            if include_payload:
                return request.model_copy(update={"payload": dict(payload)})
            return request
        raise AssertionError("unreachable: the last attempt returns or raises")

    async def _submit_once(self, terms: _Terms) -> ApprovalRequest:
        same_key = and_(
            approvals.c.requested_by == terms.requested_by.id,
            approvals.c.action == terms.action,
            approvals.c.payload_sha256 == terms.payload_sha256,
        )
        conflict: ApprovalConflictError | None = None
        async with self._session_factory.begin() as session:
            # A lapsed open request must not hold the key: close it first.
            await self._store_expired(session, same_key, terms.requested_by.id)
            row = (
                await session.execute(
                    select(*REQUEST_COLUMNS, approvals.c.payload, approvals.c.resume_url)
                    .where(same_key, approvals.c.status.in_(OPEN_STATUSES))
                    .with_for_update()
                )
            ).one_or_none()
            if row is not None:
                existing = _parsed(row)
                differs = _differences(existing, row.payload, row.resume_url, terms)
                if not differs:
                    return existing
                conflict = ApprovalConflictError(
                    f"approval {existing.id} is already open for this requester, action and "
                    f"payload, with other terms ({', '.join(differs)})",
                    existing=existing.id,
                    differs=differs,
                )
                await _audit(
                    session,
                    "approval.submit_conflict",
                    terms.requested_by.id,
                    existing.id,
                    {"approval_action": terms.action, "differs": ",".join(differs)},
                    terms.context,
                )
            else:
                now = datetime.now(UTC)
                request = _request(
                    (
                        await session.execute(
                            insert(approvals)
                            .values(
                                run_id=_run_id(terms.context),
                                action=terms.action,
                                summary=terms.summary,
                                payload=dict(terms.payload),
                                payload_sha256=terms.payload_sha256,
                                requested_by=terms.requested_by.id,
                                required_role=terms.required_role,
                                created_at=now,
                                expires_at=now + timedelta(seconds=terms.ttl_seconds),
                                delegates=terms.delegates,
                                resume_url=terms.resume_url,
                                run_context=(
                                    terms.context.as_json() if terms.context is not None else null()
                                ),
                            )
                            .returning(*REQUEST_COLUMNS)
                        )
                    ).one()
                )
                await _audit(
                    session,
                    "approval.requested",
                    terms.requested_by.id,
                    request.id,
                    {
                        "action": terms.action,
                        "expires_at": request.expires_at.isoformat(),
                        "payload_sha256": request.payload_sha256,
                        "delegates": list[JsonValue](terms.delegates),
                    },
                    terms.context,
                )
                return request
        raise conflict

    async def get(self, request_id: UUID, *, verify_payload: bool = True) -> ApprovalRequest:
        """The request, with its stored payload once that has been checked against the hash.

        Raises ApprovalIntegrityError if the stored payload is not the one the hash covers. A
        purged payload reads as None, with `payload_purged_at` set. `verify_payload=False` reads
        the request without its payload, which is not fetched: for closing one, which a requester
        must be able to do whatever is stored.
        """
        if not verify_payload:
            return await self._get_unchecked(request_id)
        query = select(*REQUEST_COLUMNS, approvals.c.payload).where(approvals.c.id == request_id)
        async with self._reading("get"):
            async with self._session_factory() as session:
                row = (await session.execute(query)).one_or_none()
            if row is None:
                raise ApprovalNotFoundError(f"approval {request_id} not found")
            request = _judged(_parsed(row), datetime.now(UTC))
            if row.payload is None:
                if request.payload_purged_at is not None:
                    return request
                raise _UnreadableRow(request_id)
            if not _covers(request, row.payload):
                raise ApprovalIntegrityError(_MISMATCH.format(id=request_id))
            return request.model_copy(update={"payload": dict(row.payload)})

    async def _get_unchecked(self, request_id: UUID) -> ApprovalRequest:
        """The request without its payload, which is not read: for closing one, which a requester
        must be able to do whatever is stored."""
        query = select(*REQUEST_COLUMNS).where(approvals.c.id == request_id)
        async with self._reading("get"):
            async with self._session_factory() as session:
                row = (await session.execute(query)).one_or_none()
            if row is None:
                raise ApprovalNotFoundError(f"approval {request_id} not found")
            return _judged(_parsed(row), datetime.now(UTC))

    async def payload_of(self, request_id: UUID) -> JsonObject:
        """The stored payload, if it is the one the hash covers (ApprovalIntegrityError if not).
        ApprovalPayloadPurgedError once it was purged."""
        query = select(*REQUEST_COLUMNS, approvals.c.payload).where(approvals.c.id == request_id)
        async with self._reading("payload_of"):
            async with self._session_factory() as session:
                row = (await session.execute(query)).one_or_none()
            if row is None:
                raise ApprovalNotFoundError(f"approval {request_id} not found")
            request = _parsed(row)
            if row.payload is None:
                if request.payload_purged_at is not None:
                    raise ApprovalPayloadPurgedError(request_id)
                raise _UnreadableRow(request_id)
            if not _covers(request, row.payload):
                raise ApprovalIntegrityError(_MISMATCH.format(id=request_id))
            return dict(row.payload)

    async def _pending_rows(
        self, principal: Principal, *, limit: int, after: UUID | None
    ) -> list[Any]:
        """Raw rows (with their stored payload) for the pending listing: unexpired, listed actions,
        a role this principal holds, not its own and not one it is a delegate of, oldest first."""
        if principal.kind is not PrincipalKind.HUMAN or not principal.roles:
            return []
        query = (
            select(*REQUEST_COLUMNS, approvals.c.payload)
            .where(
                approvals.c.status == ApprovalStatus.PENDING.value,
                approvals.c.expires_at > datetime.now(UTC),
                approvals.c.required_role.in_(sorted(principal.roles)),
                approvals.c.action.in_(self._listed_actions),
                approvals.c.requested_by != principal.id,
                # A delegate may use a request, never decide it: it is not offered to them.
                ~approvals.c.delegates.contains([principal.id]),
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
        """The rows that parse and whose payload is the one the hash covers; each one that is not
        is recorded once and left out, so an approver is never shown what the hash does not bind."""
        items: list[ApprovalRequest] = []
        for row in rows:
            request = _readable(row)
            if request is None:
                await self._record_unreadable(row.id, "listing")
            elif not _stored_payload_is_covered(request, row.payload):
                await self._record_unreadable(row.id, "listing_payload")
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
        request = await self._get_unchecked(request_id)
        if request.status is ApprovalStatus.PENDING:
            try:
                return await self.cancel(request_id, principal=principal, reason="wait ended")
            except (ApprovalAlreadyResolvedError, ApprovalExpiredError):
                return await self._get_unchecked(request_id)
        return request

    async def expire_due(
        self, *, principal: Principal, now: datetime | None = None, limit: int = EXPIRE_BATCH_MAX
    ) -> int:
        """Store EXPIRED on open requests (pending or approved) past their lifetime; returns how
        many.

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
                    approvals.c.status.in_(OPEN_STATUSES),
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

    async def purge_payloads(
        self,
        *,
        principal: Principal,
        older_than: timedelta,
        limit: int = PURGE_BATCH_MAX,
        connection: Any = None,
    ) -> int:
        """Remove the stored payload of finished requests whose finish time is older than
        `older_than` by the database's clock; returns how many.

        Finished means consumed, rejected, cancelled or expired. The finish time is `consumed_at`,
        `resolved_at` (rejected) or `closed_at`, all stamped by the database, so a caller cannot
        move one. `older_than` below the retention floor (24 hours) is a ValueError, and the
        database refuses it too. `payload_sha256` stays, so what the payload was stays bound;
        `payload_purged_at` is the database's clock. One `approval.payload_purged` audit record is
        written per request, in the same transaction. Only the approver role may purge, so this
        runs on its connection. A request another transaction holds is skipped, and the next run
        takes it, so a short batch does not always mean the backlog is empty.

        The principal must hold the approver or the admin role (`PURGE_ROLES`): any other is
        refused with NotAuthorizedToPurgeError, and the refusal is audited, before anything else
        is looked at.
        """
        if not principal.roles & PURGE_ROLES:
            await self._record_purge_denied(principal)
            raise NotAuthorizedToPurgeError(
                f"{principal.id} holds neither the approver nor the admin role; it may not purge"
            )
        if connection is not None:
            raise ConfigError("this queue runs its own transactions; it takes no connection")
        if older_than < PAYLOAD_RETENTION_FLOOR:
            raise ValueError(f"older_than must be at least {PAYLOAD_RETENTION_FLOOR}")
        if not 1 <= limit <= PURGE_BATCH_MAX:
            # The batch's audit records go in one append, which takes at most 1000.
            raise ValueError(f"limit must be between 1 and {PURGE_BATCH_MAX}")
        if self._approver_session_factory is None:
            raise ConfigError("this queue has no approver-role connection; it cannot purge")
        total = 0
        while True:
            purged = await self._purge_batch(principal, older_than, limit)
            total += purged
            if purged < limit:
                return total

    async def _record_purge_denied(self, principal: Principal) -> None:
        """Audit a refused purge. A refusal stands whether or not the note could be written."""
        try:
            async with self._session_factory.begin() as session:
                await append_in(
                    session,
                    AuditEvent(
                        action="approval.purge_denied",
                        actor_id=principal.id,
                        payload={"subject_type": "approval", "reason": "missing_role"},
                    ),
                )
        except Exception:
            log.exception("could not record the refused purge by %s", principal.id)

    async def _purge_batch(self, principal: Principal, older_than: timedelta, limit: int) -> int:
        assert self._approver_session_factory is not None
        finished_at = case(
            (approvals.c.status == ApprovalStatus.CONSUMED.value, approvals.c.consumed_at),
            (approvals.c.status == ApprovalStatus.REJECTED.value, approvals.c.resolved_at),
            else_=approvals.c.closed_at,
        )
        async with self._approver_session_factory.begin() as session:
            due = (
                await session.execute(
                    select(
                        approvals.c.id,
                        approvals.c.status,
                        approvals.c.payload_sha256,
                        approvals.c.action,
                    )
                    .where(
                        approvals.c.payload.is_not(None),
                        approvals.c.status.in_(
                            [
                                ApprovalStatus.CONSUMED.value,
                                ApprovalStatus.REJECTED.value,
                                ApprovalStatus.CANCELLED.value,
                                ApprovalStatus.EXPIRED.value,
                            ]
                        ),
                        # Epoch seconds, as the guard counts them: an interval would follow the
                        # session time zone across a DST change and could pick a row too young.
                        extract("epoch", finished_at)
                        <= extract("epoch", func.statement_timestamp())
                        - older_than.total_seconds(),
                    )
                    .order_by(finished_at, approvals.c.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for row in due:
                await session.execute(
                    update(approvals)
                    .where(approvals.c.id == row.id)
                    # The guard stamps payload_purged_at from its own clock whatever this says.
                    .values(payload=null(), payload_purged_at=func.statement_timestamp())
                )
            # One append for the batch: the chain lock is taken once, at the end, instead of
            # being held across every update while the rest of the kit's audited writes wait.
            await append_many_in(
                session,
                [
                    AuditEvent(
                        action="approval.payload_purged",
                        actor_id=principal.id,
                        subject_id=str(row.id),
                        payload={
                            "subject_type": "approval",
                            "approval_action": row.action,
                            "payload_sha256": row.payload_sha256,
                            "request_status": row.status,
                        },
                    )
                    for row in due
                ],
            )
        return len(due)

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
        """Store EXPIRED on open rows (pending, or approved and unused) matching `condition` that
        the database also finds due. An approval that lapses unused keeps its decision.

        A row whose run context cannot be parsed is still closed and audited, with the record
        saying so in place of the context."""
        due = (
            await session.execute(
                select(approvals.c.id, approvals.c.status)
                .where(
                    condition,
                    approvals.c.status.in_(OPEN_STATUSES),
                    approvals.c.expires_at <= func.statement_timestamp(),
                )
                .with_for_update()
            )
        ).all()
        if not due:
            return 0
        previous = {row.id: row.status for row in due}
        expired = (
            await session.execute(
                update(approvals)
                .where(approvals.c.id.in_(previous))
                .values(status=ApprovalStatus.EXPIRED.value, closed_at=approvals.c.expires_at)
                .returning(approvals.c.id, approvals.c.run_context)
            )
        ).all()
        for request_id, run_context in expired:
            context = _stored_context(run_context)
            note: dict[str, JsonValue] = (
                {"run_context": "unreadable"} if run_context and context is None else {}
            )
            if previous[request_id] == ApprovalStatus.APPROVED.value:
                note["previous_status"] = ApprovalStatus.APPROVED.value
            await _audit(session, "approval.expired", actor_id, request_id, note, context)
        return len(expired)
