"""The draft-approval route with idempotent submit: a repeat is not a second approval, and the
loser of a race never withdraws the approval the winner holds."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from opskit.api.routers import inbox as route
from opskit.core.ports import N8N_SERVICE

DRAFT_ID = uuid4()
OPEN = uuid4()  # the approval an identical submit returns
OTHER = uuid4()


class _Approvals:
    def __init__(self, returns: UUID) -> None:
        self.returns = returns
        self.cancelled: list[UUID] = []

    async def submit(self, **kwargs: Any) -> Any:
        assert kwargs["requested_by"] == N8N_SERVICE
        return SimpleNamespace(id=self.returns)

    async def cancel(self, approval_id: UUID, **kwargs: Any) -> None:
        self.cancelled.append(approval_id)


def _draft(status: str, approval_id: UUID | None) -> Any:
    return SimpleNamespace(
        draft_id=DRAFT_ID,
        message_id="m1",
        status=status,
        approval_id=approval_id,
        to="someone@example.example",
        subject="Hello",
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    drafts: list[Any],
    submit_returns: UUID,
    moved: bool = True,
) -> tuple[Any, _Approvals, list[str]]:
    approvals = _Approvals(submit_returns)
    calls: list[str] = []
    reads = iter(drafts)

    async def get_draft(*_: Any) -> Any:
        return next(reads)

    async def quarantined(*_: Any) -> bool:
        return False

    async def set_status(*_: Any, **__: Any) -> bool:
        calls.append("moved")
        return moved

    async def run_context(*_: Any) -> Any:
        return SimpleNamespace()

    monkeypatch.setattr(route.store, "get_draft", get_draft)
    monkeypatch.setattr(route.store, "is_quarantined", quarantined)
    monkeypatch.setattr(route.store, "set_draft_status", set_status)
    monkeypatch.setattr(route.store, "approval_payload", lambda draft: {"draft_id": "x"})
    monkeypatch.setattr(route, "_run_context", run_context)
    monkeypatch.setattr(route, "internal_resume_target", lambda *_: None)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(),
                session_factory=object(),
                core=SimpleNamespace(approvals=approvals),
            )
        )
    )
    return request, approvals, calls


def _body() -> Any:
    return route.ApprovalRequestBody(run_id=uuid4(), resume_url="http://api.example/resume")


async def test_a_retry_returns_the_open_approval_and_moves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, approvals, calls = _wire(
        monkeypatch, drafts=[_draft("pending", OPEN)], submit_returns=OPEN
    )
    created = await route.request_draft_approval(request, DRAFT_ID, _body())
    assert created.approval_id == OPEN
    assert calls == [] and approvals.cancelled == []


async def test_a_retry_whose_approval_has_closed_withdraws_the_new_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, approvals, calls = _wire(
        monkeypatch, drafts=[_draft("pending", OPEN)], submit_returns=OTHER
    )
    with pytest.raises(HTTPException) as refused:
        await route.request_draft_approval(request, DRAFT_ID, _body())
    assert refused.value.status_code == 409
    assert approvals.cancelled == [OTHER] and calls == []


async def test_the_loser_of_a_race_keeps_the_winners_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Both calls submitted the same request, so both hold the same approval; the winner moved
    # the draft to it. The loser must not withdraw it.
    request, approvals, _ = _wire(
        monkeypatch,
        drafts=[_draft("draft", None), _draft("pending", OPEN)],
        submit_returns=OPEN,
        moved=False,
    )
    created = await route.request_draft_approval(request, DRAFT_ID, _body())
    assert created.approval_id == OPEN
    assert approvals.cancelled == []


async def test_a_loser_holding_a_different_approval_withdraws_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, approvals, _ = _wire(
        monkeypatch,
        drafts=[_draft("draft", None), _draft("pending", OPEN)],
        submit_returns=OTHER,
        moved=False,
    )
    with pytest.raises(HTTPException) as refused:
        await route.request_draft_approval(request, DRAFT_ID, _body())
    assert refused.value.status_code == 409
    assert approvals.cancelled == [OTHER]
