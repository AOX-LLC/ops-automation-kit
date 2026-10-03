"""The expiry sweep gives up on a request only after repeated failures, and a success resets it."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest
from aox_agent_core.approvals import RoleApproverPolicy

from opskit.core.pg.approvals import SWEEP_ATTEMPTS_MAX, PgApprovalQueue
from opskit.core.ports import ROLES_BY_ACTION


class _Factory:
    """A session factory whose transactions open and close without a database."""

    @asynccontextmanager
    async def begin(self) -> Any:
        yield object()


def _queue() -> PgApprovalQueue:
    return PgApprovalQueue(
        _Factory(),  # type: ignore[arg-type]
        policy=RoleApproverPolicy(roles_by_action=ROLES_BY_ACTION),
        listed_actions=ROLES_BY_ACTION,
    )


async def test_a_request_is_given_up_on_only_after_repeated_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _queue()

    async def failing(*_: Any) -> int:
        raise RuntimeError("connection reset")

    monkeypatch.setattr(queue, "_store_expired", failing)
    request_id = uuid4()
    for attempt in range(1, SWEEP_ATTEMPTS_MAX):
        assert await queue._expire_one(request_id, "svc") == 0
        assert request_id not in queue._given_up_on(), f"given up after {attempt}"
    assert await queue._expire_one(request_id, "svc") == 0
    assert request_id in queue._given_up_on()


async def test_a_success_clears_the_failure_count(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = _queue()
    request_id = uuid4()
    queue._sweep_failures[request_id] = SWEEP_ATTEMPTS_MAX - 1

    async def closing(*_: Any) -> int:
        return 1

    monkeypatch.setattr(queue, "_store_expired", closing)
    assert await queue._expire_one(request_id, "svc") == 1
    assert request_id not in queue._sweep_failures
