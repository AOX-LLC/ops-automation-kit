"""The wire layer, against a stand-in for the network: what each HTTP-level failure comes to."""

from __future__ import annotations

import httpx2
import pytest
from pydantic import SecretStr

from opskit.gateway.client import classify
from opskit.gateway.mcp_transport import McpTransport
from opskit.gateway.outcomes import RateLimited, Unauthorized, Unavailable

# Built at run time so no token-shaped literal sits in the repository.
FAKE_TOKEN = "aig_" + "abcdefgh" + "_" + "A" * 43
SEARCH = {"query": "Pier Nine", "limit": 5}


def _transport_answering(status: int) -> McpTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json={"error": "refused"})

    return McpTransport(
        "http://gateway.test/mcp",
        SecretStr(FAKE_TOKEN),
        http_transport=httpx2.MockTransport(handler),
    )


async def test_http_401_from_the_gateway_is_read_as_unauthorized() -> None:
    raw = await _transport_answering(401).call("crm__search_accounts", SEARCH)

    assert (raw.kind, raw.status) == ("http_status", 401)
    assert isinstance(classify(raw), Unauthorized)


async def test_http_429_from_the_gateway_is_read_as_rate_limited() -> None:
    raw = await _transport_answering(429).call("crm__search_accounts", SEARCH)

    assert (raw.kind, raw.status) == ("http_status", 429)
    assert isinstance(classify(raw), RateLimited)


async def test_the_token_is_in_no_part_of_a_refused_outcome() -> None:
    raw = await _transport_answering(401).call("crm__search_accounts", SEARCH)

    assert FAKE_TOKEN not in repr(raw)
    assert FAKE_TOKEN not in str(raw.to_json())


async def test_a_connection_that_fails_is_unavailable_with_a_fixed_phrase() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    transport = McpTransport(
        "http://gateway.test/mcp",
        SecretStr(FAKE_TOKEN),
        http_transport=httpx2.MockTransport(refuse),
    )

    raw = await transport.call("crm__search_accounts", SEARCH)

    outcome = classify(raw)
    assert isinstance(outcome, Unavailable)
    assert outcome.reason == "connection failed"
    assert FAKE_TOKEN not in repr(raw)


async def test_a_server_error_that_is_not_a_login_refusal_is_not_called_unauthorized() -> None:
    raw = await _transport_answering(500).call("crm__search_accounts", SEARCH)

    assert not isinstance(classify(raw), Unauthorized)


def test_only_the_first_response_status_can_make_a_login_refusal() -> None:
    from mcp.shared.exceptions import MCPError

    from opskit.gateway.mcp_transport import _outcome_of_failure

    generic = MCPError(-32603, "Server returned an error response")

    assert _outcome_of_failure(generic, [401]).status == 401
    assert _outcome_of_failure(generic, [200, 200, 401]).kind == "rpc_error"


def test_a_gateway_rpc_code_is_never_overridden_by_a_status() -> None:
    from mcp.shared.exceptions import MCPError

    from opskit.gateway.mcp_transport import _outcome_of_failure

    refused = MCPError(-32010, "Request blocked by gateway policy.")

    assert _outcome_of_failure(refused, [401]).kind == "rpc_error"


async def test_a_cancelled_call_is_cancelled_not_turned_into_a_result() -> None:
    import asyncio

    async def slow(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(30)
        return httpx2.Response(200)

    transport = McpTransport(
        "http://gateway.test/mcp", SecretStr(FAKE_TOKEN), http_transport=httpx2.MockTransport(slow)
    )

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(transport.call("crm__search_accounts", SEARCH), timeout=0.3)
