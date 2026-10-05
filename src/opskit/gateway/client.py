"""The helper API's view of the gateway's four tools, as typed results.

What it enforces for itself, because the gateway would refuse the call anyway: `limit` from 1 to 5
on every search and deals call, and a ticket description of at most 1,000 characters. A ticket
should be opened with an account id and a summary, never pasted contact details: nothing here can
check that, and the gateway's egress layer is the control. What it never does: retry a pending
write, or log an argument. Every call leaves one `gateway.call` audit record holding the tool, a
hash of the arguments, the outcome and the run id, and no argument or result text; if that record
cannot be written the call raises GatewayAuditError carrying the outcome.
"""

from __future__ import annotations

import contextlib
import json
import re
from time import monotonic

from opskit.core.ports import AuditEvent, AuditLog, RunContext
from opskit.gateway.outcomes import (
    ApprovalExpired,
    ApprovalRejected,
    NotAvailable,
    Ok,
    Outcome,
    Pending,
    PolicyRefused,
    RateLimited,
    Unauthorized,
    Unavailable,
    UpstreamError,
)
from opskit.gateway.transport import JsonObject, RawOutcome, ToolTransport, arguments_sha256

AUDIT_ACTOR = "service.gateway-client"
POLICY_REFUSAL_CODE = -32010
NOT_AVAILABLE_CODE = -32602
MAX_LIMIT = 5
MAX_DESCRIPTION_CHARS = 1000
PRIORITIES = ("low", "normal", "high")

_REJECTED = "An approver rejected"
_EXPIRED = "The approval for this call expired"
_RETRY_IN = re.compile(r"Retry in (\d+) second")
_REQUEST_ID = re.compile(r"request[ _-]?id[\"':= ]+([A-Za-z0-9._-]{4,64})", re.IGNORECASE)


class GatewayArgumentError(ValueError):
    """A call this client will not make because the gateway would refuse it. A bug in the caller."""


class GatewayAuditError(Exception):
    """The gateway answered, but the audit record of the call could not be written. The call
    happened (a write may have been approved and made): `outcome` is what the gateway said, so the
    caller can act on it and must not simply repeat the call."""

    def __init__(self, outcome: Outcome) -> None:
        super().__init__(f"The call finished as {type(outcome).__name__} but could not be audited.")
        self.outcome = outcome


class GatewayClient:
    def __init__(self, transport: ToolTransport, audit: AuditLog | None = None) -> None:
        self._transport = transport
        self._audit = audit

    async def search_accounts(self, ctx: RunContext, query: str, *, limit: int) -> Outcome:
        return await self._call(
            ctx, "crm__search_accounts", {"query": query, "limit": _checked_limit(limit)}
        )

    async def get_account(self, ctx: RunContext, account_id: str) -> Outcome:
        return await self._call(ctx, "crm__get_account", {"account_id": account_id})

    async def list_deals(self, ctx: RunContext, account_id: str, *, limit: int) -> Outcome:
        return await self._call(
            ctx, "crm__list_deals", {"account_id": account_id, "limit": _checked_limit(limit)}
        )

    async def create_ticket(
        self,
        ctx: RunContext,
        *,
        account_id: str,
        subject: str,
        description: str,
        priority: str = "normal",
    ) -> Outcome:
        """Open one ticket. A person at the gateway approves it first, so expect Pending."""
        if len(description) > MAX_DESCRIPTION_CHARS:
            raise GatewayArgumentError(
                f"A ticket description is at most {MAX_DESCRIPTION_CHARS} characters here."
            )
        if priority not in PRIORITIES:
            raise GatewayArgumentError(f"priority must be one of {PRIORITIES}.")
        arguments = {
            "account_id": account_id,
            "subject": subject,
            "description": description,
            "priority": priority,
        }
        return await self._call(ctx, "tickets__create_ticket", arguments)

    async def _call(
        self, ctx: RunContext, tool: str, arguments: JsonObject, *, meta: JsonObject | None = None
    ) -> Outcome:
        started = monotonic()
        raw = await self._transport.call(tool, arguments, meta=meta)
        outcome = classify(raw)
        try:
            await self._record(ctx, tool, arguments, outcome, raw, elapsed_s=monotonic() - started)
        except Exception as error:
            raise GatewayAuditError(outcome) from error
        return outcome

    async def _record(
        self,
        ctx: RunContext,
        tool: str,
        arguments: JsonObject,
        outcome: Outcome,
        raw: RawOutcome,
        *,
        elapsed_s: float,
    ) -> None:
        if self._audit is None:
            return
        payload: dict[str, str | int] = {
            "tool": tool,
            "arguments_sha256": arguments_sha256(tool, arguments),
            "outcome": type(outcome).__name__,
            "elapsed_ms": round(elapsed_s * 1000),
        }
        request_id = gateway_request_id(raw)
        if request_id:
            payload["gateway_request_id"] = request_id
        if isinstance(outcome, Pending) and outcome.approval_id:
            payload["approval_id"] = outcome.approval_id
        await self._audit.append(
            AuditEvent(
                action="gateway.call",
                actor_id=AUDIT_ACTOR,
                subject_id=tool,
                payload=payload,
                context=ctx,
            )
        )


def _checked_limit(limit: int) -> int:
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise GatewayArgumentError(
            f"limit must be from 1 to {MAX_LIMIT}; the gateway refuses more."
        )
    return limit


def classify(raw: RawOutcome) -> Outcome:
    if raw.kind == "http_status":
        return _from_http_status(raw.status)
    if raw.kind == "unreachable":
        return Unavailable(raw.reason or "no answer")
    if raw.kind == "rpc_error":
        return _from_rpc_error(raw)
    return _from_result(raw)


def _from_http_status(status: int | None) -> Outcome:
    if status == 401:
        return Unauthorized()
    if status == 429:
        return RateLimited(retry_after_s=None)
    return Unavailable(f"HTTP {status}")


def _from_rpc_error(raw: RawOutcome) -> Outcome:
    if raw.code == NOT_AVAILABLE_CODE:
        return NotAvailable(raw.message)
    if raw.code != POLICY_REFUSAL_CODE:
        return Unavailable(f"JSON-RPC error {raw.code}")
    if raw.message.startswith(_REJECTED):
        return ApprovalRejected(raw.message)
    if raw.message.startswith(_EXPIRED):
        return ApprovalExpired(raw.message)
    retry = _RETRY_IN.search(raw.message)
    if raw.message.startswith("Too many requests"):
        return RateLimited(int(retry.group(1)) if retry else None)
    return PolicyRefused(gateway_request_id(raw), raw.message)


def _from_result(raw: RawOutcome) -> Outcome:
    if not raw.is_error:
        return Ok(raw.text, raw.structured)
    pending = _pending_marker(raw)
    if pending is not None:
        approval_id = pending.get("approval_id")
        return Pending(str(approval_id) if approval_id is not None else None)
    return UpstreamError(raw.text)


def _pending_marker(raw: RawOutcome) -> JsonObject | None:
    """The structured {"status": "approval_pending", ...}, or the same JSON in the text."""
    candidates: list[object] = [raw.structured]
    with contextlib.suppress(ValueError):
        candidates.append(json.loads(raw.text))
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("status") == "approval_pending":
            return candidate
    return None


def gateway_request_id(raw: RawOutcome) -> str | None:
    """The request id the gateway puts in a refusal (a JSON-RPC error), wherever it put it; None
    when it gave none. Result text is never searched: it can hold a customer's data."""
    if raw.kind != "rpc_error":
        return None
    if isinstance(raw.data, dict):
        for key in ("request_id", "requestId", "id"):
            value = raw.data.get(key)
            if isinstance(value, str | int):
                return str(value)
    found = _REQUEST_ID.search(raw.message)
    return found.group(1) if found else None
