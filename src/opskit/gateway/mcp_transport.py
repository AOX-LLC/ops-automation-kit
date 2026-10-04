"""The wire: MCP over streamable HTTP (protocol 2025-11-25) to the gateway.

The only module that imports the MCP SDK or its HTTP client (a test and an import-linter contract
hold that). The bearer token lives in the Authorization header of one HTTP client and nowhere
else: nothing here logs, formats or re-raises it, and a failure is reduced to a RawOutcome with a
fixed phrase, never the SDK's exception text.
"""

from __future__ import annotations

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


class McpTransport:
    def __init__(self, url: str, token: SecretStr, timeout_s: float = 60.0) -> None:
        if timeout_s < MIN_TIMEOUT_S:
            raise ValueError(
                f"The timeout must exceed the gateway's 45 s approval hold ({MIN_TIMEOUT_S} s)."
            )
        self._url = url
        self._token = token
        self._timeout_s = timeout_s
        # Class names only, for a person debugging a live run; never a message.
        self.last_failure_types: tuple[str, ...] = ()

    async def call(
        self, tool: str, arguments: JsonObject, *, meta: JsonObject | None = None
    ) -> RawOutcome:
        try:
            async with (
                httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {self._token.get_secret_value()}"},
                    timeout=self._timeout_s,
                ) as http_client,
                Client(
                    streamable_http_client(self._url, http_client=http_client),
                    mode="legacy",
                    read_timeout_seconds=self._timeout_s,
                ) as client,
            ):
                result = await client.call_tool(
                    tool,
                    arguments,
                    read_timeout_seconds=self._timeout_s,
                    meta=cast(RequestParamsMeta | None, meta),
                )
        except (Exception, BaseExceptionGroup) as error:
            self.last_failure_types = tuple(type(leaf).__name__ for leaf in _leaves(error))
            return _outcome_of_failure(error)
        return RawOutcome(
            kind="result",
            is_error=bool(result.is_error),
            text="\n".join(b.text for b in result.content if isinstance(b, TextContent)),
            structured=result.structured_content,
        )


def _leaves(error: BaseException) -> Iterator[BaseException]:
    """The real failures inside the exception groups the SDK's task groups wrap them in."""
    if isinstance(error, BaseExceptionGroup):
        for inner in error.exceptions:
            yield from _leaves(inner)
    else:
        yield error


def _outcome_of_failure(error: BaseException) -> RawOutcome:
    leaves = list(_leaves(error))
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
