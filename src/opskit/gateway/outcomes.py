"""What a gateway call can come to, as values. The client returns one of these and never raises
for anything the gateway does; a pending write is a normal state, not an error."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

type JsonObject = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Ok:
    """The call went through. `data` is the tool's structured result when it gave one."""

    text: str
    data: JsonObject | None = None


@dataclass(frozen=True, slots=True)
class Pending:
    """A write is waiting for a person. Nothing was forwarded. Calling again asks the gateway for
    the same request; this client never does that by itself."""

    approval_id: str | None


@dataclass(frozen=True, slots=True)
class PolicyRefused:
    """Blocked by a gateway layer (-32010). The gateway names no reason, only a request id."""

    request_id: str | None
    message: str


@dataclass(frozen=True, slots=True)
class ApprovalRejected:
    """An approver rejected this call. The rejection stands until the request expires."""

    message: str


@dataclass(frozen=True, slots=True)
class ApprovalExpired:
    message: str


@dataclass(frozen=True, slots=True)
class NotAvailable:
    """Unknown tool, or one outside this client's scope (-32602; the gateway gives one answer)."""

    message: str


@dataclass(frozen=True, slots=True)
class RateLimited:
    retry_after_s: int | None


@dataclass(frozen=True, slots=True)
class Unauthorized:
    """HTTP 401: the token is wrong, revoked or expired."""


@dataclass(frozen=True, slots=True)
class UpstreamError:
    """The tool server failed behind the gateway (an isError result that is not a pending write)."""

    text: str


@dataclass(frozen=True, slots=True)
class Unavailable:
    """No usable answer: the gateway was unreachable, timed out or answered something unexpected.
    `reason` is a fixed short phrase, never exception text."""

    reason: str


type Outcome = (
    Ok
    | Pending
    | PolicyRefused
    | ApprovalRejected
    | ApprovalExpired
    | NotAvailable
    | RateLimited
    | Unauthorized
    | UpstreamError
    | Unavailable
)
