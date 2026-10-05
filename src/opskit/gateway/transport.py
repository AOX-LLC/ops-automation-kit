"""The seam between the client and the wire: a call goes in, a RawOutcome comes out.

A RawOutcome is exactly what the gateway said, before the client reads any meaning into it, so a
recording of it stays valid when the client's reading of it changes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from typing import Any, Literal, Protocol, get_args

type JsonObject = dict[str, Any]
type OutcomeKind = Literal["result", "rpc_error", "http_status", "unreachable"]

PROTOCOL_VERSION = "2025-11-25"


@dataclass(frozen=True, slots=True)
class RawOutcome:
    kind: OutcomeKind
    # kind == "result": the tool result.
    is_error: bool = False
    text: str = ""
    structured: JsonObject | None = None
    # The result's own _meta, when the gateway sent one (a place a request id could appear).
    meta: JsonObject | None = None
    # kind == "rpc_error": a JSON-RPC error.
    code: int | None = None
    message: str = ""
    data: Any = None
    # kind == "http_status": the answer was an HTTP error before any JSON-RPC.
    status: int | None = None
    # kind == "unreachable": a fixed phrase, never exception text.
    reason: str = ""

    def to_json(self) -> JsonObject:
        document = asdict(self)
        return {k: v for k, v in document.items() if k == "kind" or v not in (None, "", False)}

    @classmethod
    def from_json(cls, document: JsonObject) -> RawOutcome:
        known = {f.name for f in fields(cls)}
        if set(document) - known:
            raise ValueError(f"unknown outcome fields: {sorted(set(document) - known)}")
        if document.get("kind") not in get_args(OutcomeKind.__value__):
            raise ValueError(f"unknown outcome kind: {document.get('kind')!r}")
        return cls(**document)


class ToolTransport(Protocol):
    async def call(
        self, tool: str, arguments: JsonObject, *, meta: JsonObject | None = None
    ) -> RawOutcome: ...


def call_key(tool: str, arguments: JsonObject, scenario: str) -> str:
    """The recording a call belongs to. The scenario separates calls that are the same on the wire
    but meet a different gateway state, such as a call made with a bad token."""
    canonical = json.dumps(
        {"tool": tool, "arguments": arguments, "scenario": scenario},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def arguments_sha256(tool: str, arguments: JsonObject) -> str:
    """What the audit log keeps in place of the arguments."""
    return call_key(tool, arguments, "")
