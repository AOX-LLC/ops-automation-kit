from typing import Any

import pytest

from opskit.core.ports import AuditEvent, RunContext
from opskit.gateway import (
    ApprovalExpired,
    ApprovalRejected,
    GatewayArgumentError,
    GatewayClient,
    NotAvailable,
    Ok,
    Pending,
    PolicyRefused,
    RateLimited,
    Unauthorized,
    Unavailable,
    UpstreamError,
)
from opskit.gateway.client import classify, gateway_request_id
from opskit.gateway.transport import JsonObject, RawOutcome

CTX = RunContext(run_id="run-1")
MARKER = "MARKER-ARG-xyz"
PENDING_RESULT = RawOutcome(
    kind="result",
    is_error=True,
    structured={"status": "approval_pending", "approval_id": "abc"},
)


class FakeTransport:
    def __init__(self, *outcomes: RawOutcome) -> None:
        self.queue = list(outcomes)
        self.calls: list[tuple[str, JsonObject]] = []

    async def call(
        self, tool: str, arguments: JsonObject, *, meta: JsonObject | None = None
    ) -> RawOutcome:
        self.calls.append((tool, arguments))
        return self.queue.pop(0)


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def append(self, event: AuditEvent) -> Any:
        self.events.append(event)


def ok_result() -> RawOutcome:
    return RawOutcome(kind="result", text="fine")


def rpc_error(code: int, message: str, data: Any = None) -> RawOutcome:
    return RawOutcome(kind="rpc_error", code=code, message=message, data=data)


@pytest.mark.parametrize("method", ["search_accounts", "list_deals"])
@pytest.mark.parametrize("limit", [0, 6, -1])
async def test_a_limit_outside_one_to_five_is_refused_before_the_transport(
    method: str, limit: int
) -> None:
    transport = FakeTransport()
    client = GatewayClient(transport)
    with pytest.raises(GatewayArgumentError):
        await getattr(client, method)(CTX, "x", limit=limit)
    assert transport.calls == []


@pytest.mark.parametrize("limit", [1, 5])
async def test_search_accounts_sends_an_allowed_limit(limit: int) -> None:
    transport = FakeTransport(ok_result())
    await GatewayClient(transport).search_accounts(CTX, "acme", limit=limit)
    assert transport.calls == [("crm__search_accounts", {"query": "acme", "limit": limit})]


@pytest.mark.parametrize("limit", [1, 5])
async def test_list_deals_sends_an_allowed_limit(limit: int) -> None:
    transport = FakeTransport(ok_result())
    await GatewayClient(transport).list_deals(CTX, "acct-1", limit=limit)
    assert transport.calls == [("crm__list_deals", {"account_id": "acct-1", "limit": limit})]


async def test_create_ticket_refuses_a_description_over_1000_characters() -> None:
    transport = FakeTransport()
    with pytest.raises(GatewayArgumentError):
        await GatewayClient(transport).create_ticket(
            CTX, account_id="a", subject="s", description="x" * 1001
        )
    assert transport.calls == []


async def test_create_ticket_accepts_a_description_of_exactly_1000_characters() -> None:
    transport = FakeTransport(PENDING_RESULT)
    await GatewayClient(transport).create_ticket(
        CTX, account_id="a", subject="s", description="x" * 1000
    )
    assert len(transport.calls) == 1


async def test_create_ticket_refuses_an_unknown_priority() -> None:
    transport = FakeTransport()
    with pytest.raises(GatewayArgumentError):
        await GatewayClient(transport).create_ticket(
            CTX, account_id="a", subject="s", description="d", priority="urgent"
        )
    assert transport.calls == []


async def test_create_ticket_sends_account_subject_description_and_priority() -> None:
    transport = FakeTransport(PENDING_RESULT)
    await GatewayClient(transport).create_ticket(
        CTX, account_id="acct-1", subject="Sub", description="Desc", priority="high"
    )
    assert transport.calls == [
        (
            "tickets__create_ticket",
            {
                "account_id": "acct-1",
                "subject": "Sub",
                "description": "Desc",
                "priority": "high",
            },
        )
    ]


def test_an_error_result_with_the_pending_marker_is_pending() -> None:
    assert classify(PENDING_RESULT) == Pending("abc")


def test_the_pending_marker_in_the_text_alone_is_pending() -> None:
    raw = RawOutcome(
        kind="result",
        is_error=True,
        text='{"status": "approval_pending", "approval_id": "abc"}',
    )
    assert classify(raw) == Pending("abc")


def test_an_error_result_without_the_marker_is_an_upstream_error() -> None:
    raw = RawOutcome(kind="result", is_error=True, text="boom")
    assert classify(raw) == UpstreamError("boom")


def test_a_normal_result_is_ok_with_text_and_structured_data() -> None:
    raw = RawOutcome(kind="result", text="hello", structured={"a": 1})
    assert classify(raw) == Ok("hello", {"a": 1})


def test_a_policy_block_is_policy_refused() -> None:
    message = "Request blocked by gateway policy."
    assert classify(rpc_error(-32010, message)) == PolicyRefused(None, message)


def test_a_rejected_approval_is_approval_rejected() -> None:
    message = "An approver rejected this call."
    assert classify(rpc_error(-32010, message)) == ApprovalRejected(message)


def test_an_expired_approval_is_approval_expired() -> None:
    message = "The approval for this call expired."
    assert classify(rpc_error(-32010, message)) == ApprovalExpired(message)


def test_too_many_requests_carries_the_retry_seconds() -> None:
    message = "Too many requests. Retry in 7 seconds."
    assert classify(rpc_error(-32010, message)) == RateLimited(7)


def test_code_32602_is_not_available() -> None:
    assert classify(rpc_error(-32602, "Unknown tool")) == NotAvailable("Unknown tool")


def test_any_other_rpc_code_is_unavailable() -> None:
    assert isinstance(classify(rpc_error(-32000, "odd")), Unavailable)


def test_http_401_is_unauthorized() -> None:
    assert classify(RawOutcome(kind="http_status", status=401)) == Unauthorized()


def test_http_429_is_rate_limited_without_a_retry_time() -> None:
    assert classify(RawOutcome(kind="http_status", status=429)) == RateLimited(None)


def test_http_500_is_unavailable() -> None:
    assert isinstance(classify(RawOutcome(kind="http_status", status=500)), Unavailable)


def test_unreachable_keeps_its_reason() -> None:
    raw = RawOutcome(kind="unreachable", reason="timed out")
    assert classify(raw) == Unavailable("timed out")


def test_the_request_id_is_found_in_the_rpc_data() -> None:
    assert gateway_request_id(rpc_error(-32010, "Blocked.", {"request_id": "r-123"})) == "r-123"


def test_the_request_id_is_found_in_the_message_text() -> None:
    message = "Request blocked by gateway policy. (request id: r-9f2)"
    assert gateway_request_id(rpc_error(-32010, message)) == "r-9f2"


def test_the_request_id_is_none_when_absent() -> None:
    assert gateway_request_id(rpc_error(-32010, "Request blocked by gateway policy.")) is None


async def test_a_pending_call_is_not_retried() -> None:
    transport = FakeTransport(PENDING_RESULT, PENDING_RESULT)
    outcome = await GatewayClient(transport).create_ticket(
        CTX, account_id="a", subject="s", description="d"
    )
    assert outcome == Pending("abc")
    assert len(transport.calls) == 1


async def test_each_call_leaves_one_audit_event_with_exactly_the_expected_keys() -> None:
    audit = FakeAudit()
    transport = FakeTransport(ok_result())
    await GatewayClient(transport, audit).get_account(CTX, "acct-1")
    (event,) = audit.events
    assert event.action == "gateway.call"
    assert set(event.payload) == {"tool", "arguments_sha256", "outcome", "elapsed_ms"}
    assert event.payload["tool"] == "crm__get_account"
    assert event.payload["outcome"] == "Ok"


async def test_the_audit_event_keeps_the_run_id() -> None:
    audit = FakeAudit()
    await GatewayClient(FakeTransport(ok_result()), audit).get_account(CTX, "acct-1")
    assert audit.events[0].context == CTX
    assert audit.events[0].context.run_id == "run-1"


async def test_a_pending_audit_event_adds_the_approval_id() -> None:
    audit = FakeAudit()
    await GatewayClient(FakeTransport(PENDING_RESULT), audit).create_ticket(
        CTX, account_id="a", subject="s", description="d"
    )
    assert audit.events[0].payload["approval_id"] == "abc"
    assert audit.events[0].payload["outcome"] == "Pending"


async def test_a_refusal_audit_event_adds_the_gateway_request_id() -> None:
    audit = FakeAudit()
    refusal = rpc_error(-32010, "Request blocked by gateway policy.", {"request_id": "r-123"})
    await GatewayClient(FakeTransport(refusal), audit).get_account(CTX, "acct-1")
    assert audit.events[0].payload["gateway_request_id"] == "r-123"


async def test_no_argument_value_reaches_the_audit_event() -> None:
    audit = FakeAudit()
    client = GatewayClient(FakeTransport(PENDING_RESULT, ok_result()), audit)
    await client.create_ticket(CTX, account_id="a", subject="s", description=MARKER)
    await client.search_accounts(CTX, MARKER, limit=1)
    assert len(audit.events) == 2
    for event in audit.events:
        assert MARKER not in event.model_dump_json()


async def test_a_client_without_an_audit_log_still_answers() -> None:
    outcome = await GatewayClient(FakeTransport(ok_result()), None).get_account(CTX, "a")
    assert outcome == Ok("fine", None)
