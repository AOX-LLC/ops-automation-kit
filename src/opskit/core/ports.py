"""Interfaces for model calls, approvals, audit and run records.

Shaped after agent-core's planned public interfaces so Phase 2 can swap the local
stub for the pinned library by changing only `factory.py`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type JsonObject = dict[str, Any]


class Mode(StrEnum):
    MOCK = "mock"
    LIVE = "live"
    RECORD = "record"


class Tier(StrEnum):
    SMALL = "small"
    MID = "mid"
    LARGE = "large"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class Decision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class RunContext:
    run_id: UUID
    workflow: str
    mode: Mode
    external_ids: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class PromptRef:
    id: str
    version: int
    template: str


@dataclass(frozen=True, slots=True)
class Attachment:
    media_type: Literal["image/png", "image/jpeg", "application/pdf"]
    data: bytes


@dataclass(frozen=True, slots=True)
class ModelResult[T: BaseModel]:
    output: T
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    latency_ms: int
    fixture_key: str
    mode: Mode


@dataclass(frozen=True, slots=True)
class Approval:
    id: UUID
    run_id: UUID
    kind: str
    subject: JsonObject
    edited_subject: JsonObject | None
    status: ApprovalStatus
    requested_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    decided_by: str | None
    decision_note: str | None


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: Sequence[T]
    next_cursor: str | None


class ModelClient(Protocol):
    async def structured[T: BaseModel](
        self,
        *,
        ctx: RunContext,
        prompt: PromptRef,
        tier: Tier,
        schema: type[T],
        inputs: Mapping[str, JsonValue],
        attachments: Sequence[Attachment] = (),
    ) -> ModelResult[T]: ...


class ApprovalQueue(Protocol):
    async def request(
        self,
        *,
        ctx: RunContext,
        kind: str,
        subject: JsonObject,
        resume_url: str,
        expires_in: timedelta,
    ) -> Approval: ...

    async def decide(
        self,
        approval_id: UUID,
        *,
        decision: Decision,
        actor: str,
        note: str | None = None,
        edited_subject: JsonObject | None = None,
    ) -> Approval: ...

    async def get(self, approval_id: UUID) -> Approval: ...

    async def list_pending(
        self, *, limit: int = 50, cursor: str | None = None
    ) -> Page[Approval]: ...

    async def expire(self, approval_id: UUID) -> Approval: ...

    async def expire_due(self, *, now: datetime) -> int: ...


class AuditLog(Protocol):
    async def append(
        self,
        *,
        ctx: RunContext | None,
        actor: str,
        action: str,
        subject_type: str | None,
        subject_id: str | None,
        details: JsonObject,
    ) -> None: ...


class RunStore(Protocol):
    async def start(
        self, *, workflow: str, n8n_workflow_id: str | None, n8n_execution_id: str | None
    ) -> RunContext: ...

    async def get(self, run_id: UUID) -> RunContext: ...

    async def finish(self, run_id: UUID, *, succeeded: bool) -> None: ...


@dataclass(frozen=True, slots=True)
class Core:
    models: ModelClient
    approvals: ApprovalQueue
    audit: AuditLog
    runs: RunStore
