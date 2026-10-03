"""Builds the Core: agent-core's model client, plus the kit's own Postgres backends."""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any

from aox_agent_core import AgentClient, load_config
from aox_agent_core.approvals import RoleApproverPolicy

from opskit.config import Settings
from opskit.core.pg import outbox
from opskit.core.pg.approvals import PgApprovalQueue
from opskit.core.pg.audit import PgAuditLog
from opskit.core.pg.metered import MeteredModelClient
from opskit.core.pg.outbox import ResumeSender
from opskit.core.pg.runs import PgRunStore
from opskit.core.ports import ROLES_BY_ACTION, Core, Mode
from opskit.db.engine import SessionFactory


def build_model_client(settings: Settings) -> AgentClient:
    """agent-core's client from the kit's config file. Replay needs no key and never spends."""
    config = load_config(settings.agent_core_config)
    key = settings.anthropic_api_key
    if config.mode is Mode.REPLAY or key is None:
        return AgentClient(config)
    return AgentClient(config, api_key=key)


def build_core(
    settings: Settings,
    session_factory: SessionFactory,
    approver_session_factory: SessionFactory | None = None,
) -> Core:
    """`approver_session_factory` is the approver role's; only the api passes it."""
    client = build_model_client(settings)
    return Core(
        models=MeteredModelClient(client, session_factory),
        approvals=PgApprovalQueue(
            session_factory,
            policy=RoleApproverPolicy(roles_by_action=ROLES_BY_ACTION),
            approver_session_factory=approver_session_factory,
        ),
        audit=PgAuditLog(session_factory),
        runs=PgRunStore(session_factory, client.config.mode),
        mode=client.config.mode,
    )


def build_resume_worker(
    session_factory: SessionFactory, send: ResumeSender
) -> Coroutine[Any, Any, None]:
    """The background loop that delivers approval decisions back to waiting n8n executions."""
    return outbox.run_forever(session_factory, send)
