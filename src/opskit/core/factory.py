"""Builds the Core. Phase 2 swaps these stubs for the pinned agent-core release here."""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any

from opskit.config import Settings
from opskit.core.ports import Core
from opskit.core.stub import resume_outbox
from opskit.core.stub.approvals_pg import PgApprovalQueue
from opskit.core.stub.audit_pg import PgAuditLog
from opskit.core.stub.model_replay import ReplayModelClient
from opskit.core.stub.resume_outbox import ResumeSender
from opskit.core.stub.runs_pg import PgRunStore
from opskit.db.engine import SessionFactory


def build_core(settings: Settings, session_factory: SessionFactory) -> Core:
    return Core(
        models=ReplayModelClient(
            fixtures_dir=settings.fixtures_dir,
            session_factory=session_factory,
            mode=settings.mode,
        ),
        approvals=PgApprovalQueue(session_factory),
        audit=PgAuditLog(session_factory),
        runs=PgRunStore(session_factory, settings.mode),
    )


def build_resume_worker(
    session_factory: SessionFactory, send: ResumeSender
) -> Coroutine[Any, Any, None]:
    """The background loop that delivers approval decisions back to waiting n8n executions."""
    return resume_outbox.run_forever(session_factory, send)
