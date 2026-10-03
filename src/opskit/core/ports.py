"""The kit's view of models, approvals, audit and runs, backed by agent-core v0.1.0a2.

Everything outside `opskit.core` imports these names from here, never from agent-core
directly (import-linter enforces it), so the pinned library can change behind this module.
The name mapping from the Phase 1 draft follows agent-core's docs/compat-03.md.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from aox_agent_core import (
    Attachment,
    CallResult,
    Mode,
    ModelClient,
    PromptRef,
    RunContext,
    Tier,
    Usage,
)
from aox_agent_core.approvals import (
    ApprovalQueue,
    ApprovalRequest,
    ApprovalStatus,
    Decision,
    Principal,
    PrincipalKind,
)
from aox_agent_core.audit import AuditEvent, AuditHead, AuditLog, AuditRecord
from pydantic import JsonValue

__all__ = [
    "APPROVER",
    "APPROVER_ROLE",
    "N8N_SERVICE",
    "ApprovalQueue",
    "ApprovalRequest",
    "ApprovalStatus",
    "Attachment",
    "AuditEvent",
    "AuditHead",
    "AuditLog",
    "AuditRecord",
    "CallResult",
    "Core",
    "Decision",
    "JsonObject",
    "JsonValue",
    "KitApprovalQueue",
    "Mode",
    "ModelClient",
    "Page",
    "Principal",
    "PrincipalKind",
    "PromptRef",
    "RunContext",
    "RunStore",
    "Tier",
    "Usage",
    "run_uuid",
]

type JsonObject = dict[str, Any]

# Who acts in the kit. The approver page authenticates a human and acts as APPROVER;
# n8n authenticates with the service token and acts as N8N_SERVICE, which can request
# approvals but never resolve them (agent-core's RoleApproverPolicy needs a human).
APPROVER_ROLE = "approver"
APPROVER = Principal(id="approver", kind=PrincipalKind.HUMAN, roles=frozenset({APPROVER_ROLE}))
N8N_SERVICE = Principal(id="service.n8n", kind=PrincipalKind.SERVICE)


def run_uuid(ctx: RunContext) -> UUID:
    """The kit's run ids are UUIDs; agent-core carries them as opaque strings."""
    return UUID(ctx.run_id)


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: Sequence[T]
    next_cursor: str | None


class KitApprovalQueue(ApprovalQueue, Protocol):
    """agent-core's ApprovalQueue plus what the kit keeps in its own adapter.

    submit() also takes `resume_url`, the signed n8n URL to call once decided; it is
    stored but never returned. The kit also keeps an expiry sweep and a string-cursor
    page for the approver page, and shows the approver the payload it is approving.
    """

    async def submit(
        self,
        *,
        action: str,
        summary: str,
        payload: Mapping[str, JsonValue],
        requested_by: Principal,
        required_role: str,
        ttl_seconds: int,
        context: RunContext | None = None,
        resume_url: str | None = None,
    ) -> ApprovalRequest: ...

    async def payload_of(self, request_id: UUID) -> JsonObject: ...

    async def list_pending_page(
        self, principal: Principal, *, limit: int = 50, cursor: str | None = None
    ) -> Page[ApprovalRequest]: ...

    async def expire(self, request_id: UUID) -> ApprovalRequest: ...

    async def expire_due(self, *, now: datetime) -> int: ...


class RunStore(Protocol):
    async def start(
        self, *, workflow: str, n8n_workflow_id: str | None, n8n_execution_id: str | None
    ) -> RunContext: ...

    async def get(self, run_id: UUID) -> RunContext: ...

    async def finish(self, run_id: UUID, *, succeeded: bool) -> None: ...


@dataclass(frozen=True, slots=True)
class Core:
    models: ModelClient
    approvals: KitApprovalQueue
    audit: AuditLog
    runs: RunStore
    mode: Mode
