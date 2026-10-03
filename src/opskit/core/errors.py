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
    "ApprovalUnreadableError",
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


class ApprovalUnreadableError(ApprovalError):
    """The stored approval cannot be parsed. It was left alone and recorded once in the audit
    log; nothing was decided, used or withdrawn. Readers fail closed on it, row by row."""

    def __init__(self, approval_id: UUID) -> None:
        super().__init__(f"approval {approval_id} is stored in a form this application cannot read")
        self.approval_id = approval_id
