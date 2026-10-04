"""The schedule triggers can be switched off without touching a webhook call."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from opskit.api.routers import runs as runs_route
from opskit.core.ports import Mode


class _Runs:
    def __init__(self) -> None:
        self.started: list[str] = []

    async def start(self, *, workflow: str, **_: Any) -> Any:
        self.started.append(workflow)
        return SimpleNamespace(run_id="00000000-0000-0000-0000-000000000001")


def _request(scheduled_runs: bool) -> tuple[Any, _Runs]:
    runs = _Runs()
    core = SimpleNamespace(runs=runs, mode=Mode.REPLAY)
    state = SimpleNamespace(core=core, settings=SimpleNamespace(scheduled_runs=scheduled_runs))
    return SimpleNamespace(app=SimpleNamespace(state=state)), runs


def _start(scheduled_runs: bool, *, scheduled: bool) -> tuple[Any, _Runs]:
    request, runs = _request(scheduled_runs)
    body = runs_route.StartRun(workflow="receipts", scheduled=scheduled)
    return asyncio.run(runs_route.start_run(request, body)), runs


def test_a_scheduled_start_is_skipped_when_schedules_are_off() -> None:
    started, runs = _start(False, scheduled=True)
    assert started.skipped is True
    assert started.run_id is None
    assert runs.started == []


def test_a_webhook_start_still_runs_when_schedules_are_off() -> None:
    started, runs = _start(False, scheduled=False)
    assert started.skipped is False
    assert started.run_id is not None
    assert runs.started == ["receipts"]


def test_a_scheduled_start_runs_when_schedules_are_on() -> None:
    started, runs = _start(True, scheduled=True)
    assert started.skipped is False
    assert runs.started == ["receipts"]
