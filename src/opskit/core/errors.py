"""Errors raised by the adapter. Callers map them to HTTP responses."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID


class CoreError(Exception):
    """Base class for adapter errors."""


class FixtureMissing(CoreError):
    def __init__(self, key: str, expected_path: Path) -> None:
        super().__init__(
            f"no recorded response for fixture key {key}; expected it at {expected_path}. "
            "Record it with MOCK_MODE=false RECORD_FIXTURES=true and your own key."
        )
        self.key = key
        self.expected_path = expected_path


class LiveModeUnavailable(CoreError):
    def __init__(self) -> None:
        super().__init__(
            "live model calls land with the agent-core pin in Phase 2; use MOCK_MODE=true"
        )


class NotFound(CoreError):
    def __init__(self, kind: str, item_id: UUID) -> None:
        super().__init__(f"{kind} {item_id} not found")


class ApprovalNotPending(CoreError):
    def __init__(self, approval_id: UUID, status: str) -> None:
        super().__init__(f"approval {approval_id} is already {status}")
        self.status = status


class ApprovalExpired(CoreError):
    def __init__(self, approval_id: UUID) -> None:
        super().__init__(f"approval {approval_id} has expired")
