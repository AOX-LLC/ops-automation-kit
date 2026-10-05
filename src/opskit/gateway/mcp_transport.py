"""The wire: MCP over streamable HTTP (protocol 2025-11-25) to the gateway.

The only module that imports the MCP SDK or its HTTP client (a test and an import-linter contract
hold that). The bearer token lives in the Authorization header of one HTTP client and nowhere
else: nothing here logs, formats or re-raises it, and a failure is reduced to a RawOutcome with a
fixed phrase, never the SDK's exception text.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from typing import cast

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import RequestParamsMeta, TextContent
from pydantic import SecretStr

from opskit.gateway.transport import JsonObject, RawOutcome

# Longer than the gateway's 45 s approval hold, so a held write answers before we give up.
MIN_TIMEOUT_S = 50.0
# Each call is a fresh session (initialize, tools/list, tools/call, close), and the timeout bounds
# each HTTP round, so the whole call also has a deadline of its own.
CALL_DEADLINE_MARGIN_S = 15.0
# A result longer than this is not read: it would go whole into a result and a recording.
MAX_TEXT_CHARS = 1_000_000

# The SDK logs tool arguments, and results, at DEBUG. Keep both loggers quiet whatever the app sets.
for _name in ("mcp", "httpx2"):
    logging.getLogger(_name).setLevel(logging.WARNING)


# HTTP statuses the gateway answers before any JSON-RPC: 401 for a bad, revoked or expired token,
# 429 for too many failed logins. The SDK folds both into a generic -32603 "Server returned an
# error response", so they are read from the HTTP layer.
HTTP_REFUSALS = (401, 429)
# JSON-RPC codes the gateway itself answers with (HTTP 200): never overridden by an HTTP status.
GATEWAY_RPC_CODES = (-32010, -32602)


class McpTransport:
    def __init__(
        self,
        url: str,
        token: SecretStr,
        timeout_s: float = 60.0,
        *,
        http_transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        if timeout_s < MIN_TIMEOUT_S:
            raise ValueError(
                f"The timeout must exceed the gateway's 45 s approval hold ({MIN_TIMEOUT_S} s)."
            )
        self._url = url
        self._token = token
        self._timeout_s = timeout_s
        self._http_transport = http_transport  # tests only: a stand-in for the network
        # The protocol version the last session negotiated, as the SDK reports it.
        self.protocol_version: str | None = None
        # Class names only, for a person debugging a live run; never a message.
        self.last_failure_types: tuple[str, ...] = ()

    async def call(
        self, tool: str, arguments: JsonObject, *, meta: JsonObject | None = None
    ) -> RawOutcome:
        statuses: list[int] = []

        async def note_status(response: httpx2.Response) -> None:
            statuses.append(response.status_code)  # the status only, never a header

        try:
            async with asyncio.timeout(self._timeout_s + CALL_DEADLINE_MARGIN_S):
                async with (
                    httpx2.AsyncClient(
                        headers={"Authorization": f"Bearer {self._token.get_secret_value()}"},
                        timeout=self._timeout_s,
                        event_hooks={"response": [note_status]},
                        transport=self._http_transport,
                        trust_env=False,  # no proxy from the environment sees the bearer header
                    ) as http_client,
                    Client(
                        streamable_http_client(self._url, http_client=http_client),
                        mode="legacy",
                        read_timeout_seconds=self._timeout_s,
                    ) as client,
                ):
                    self.protocol_version = str(client.protocol_version)
                    result = await client.call_tool(
                        tool,
                        arguments,
                        read_timeout_seconds=self._timeout_s,
                        meta=cast(RequestParamsMeta | None, meta),
                    )
        except Exception as error:  # ExceptionGroup is one; cancellation and exit are not
            self.last_failure_types = tuple(type(leaf).__name__ for leaf in _leaves(error))
            return _outcome_of_failure(error, statuses)
        text = "\n".join(b.text for b in result.content if isinstance(b, TextContent))
        if len(text) > MAX_TEXT_CHARS:
            return RawOutcome(kind="unreachable", reason="response too large")
        return RawOutcome(
            kind="result",
            is_error=bool(result.is_error),
            text=text,
            structured=result.structured_content,
            meta=dict(result.meta) if result.meta else None,
        )


def _leaves(error: BaseException) -> Iterator[BaseException]:
    """The real failures inside the exception groups the SDK's task groups wrap them in."""
    if isinstance(error, BaseExceptionGroup):
        for inner in error.exceptions:
            yield from _leaves(inner)
    else:
        yield error


def _outcome_of_failure(error: BaseException, statuses: list[int]) -> RawOutcome:
    leaves = list(_leaves(error))
    gateway_answered = any(
        isinstance(leaf, MCPError) and leaf.code in GATEWAY_RPC_CODES for leaf in leaves
    )
    # The token is checked on the first request of a session, so a 401 or 429 is that one's status.
    refusal = statuses[0] if statuses and statuses[0] in HTTP_REFUSALS else None
    if refusal is not None and not gateway_answered:
        return RawOutcome(kind="http_status", status=refusal)
    for leaf in leaves:
        if isinstance(leaf, MCPError):
            return RawOutcome(
                kind="rpc_error", code=leaf.code, message=leaf.message, data=leaf.data
            )
    for leaf in leaves:
        response = getattr(leaf, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return RawOutcome(kind="http_status", status=status)
    for leaf in leaves:
        if isinstance(leaf, TimeoutError | httpx2.TimeoutException):
            return RawOutcome(kind="unreachable", reason="timed out")
        if isinstance(leaf, httpx2.TransportError | OSError):
            return RawOutcome(kind="unreachable", reason="connection failed")
    return RawOutcome(kind="unreachable", reason="unexpected failure")
