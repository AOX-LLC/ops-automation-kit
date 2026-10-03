"""Errors callers may catch, re-exported so nothing outside opskit.core imports agent-core."""

from __future__ import annotations

from uuid import UUID

from aox_agent_core.errors import (
    AgentCoreError,
    ApprovalAlreadyResolvedError,
    ApprovalError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ApprovalNotGrantedError,
    ApprovalPayloadMismatchError,
    AttachmentError,
    AuditIntegrityError,
    AuditPayloadRejectedError,
    ModelRefusalError,
    NotAuthorizedToResolveError,
    ReplayMissError,
    StaleRecordingError,
    StructuredOutputError,
)

__all__ = [
    "AgentCoreError",
    "ApprovalAlreadyResolvedError",
    "ApprovalError",
    "ApprovalExpiredError",
    "ApprovalNotFoundError",
    "ApprovalNotGrantedError",
    "ApprovalPayloadMismatchError",
    "AttachmentError",
    "AuditIntegrityError",
    "AuditPayloadRejectedError",
    "CoreError",
    "ModelRefusalError",
    "NotAuthorizedToResolveError",
    "NotFound",
    "ReplayMissError",
    "StaleRecordingError",
    "StructuredOutputError",
]


class CoreError(Exception):
    """Base class for errors raised by the kit's own adapter code."""


class NotFound(CoreError):
    def __init__(self, kind: str, item_id: UUID) -> None:
        super().__init__(f"{kind} {item_id} not found")
