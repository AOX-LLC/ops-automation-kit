"""The research route and the database bounds: one transaction, and an honest failure."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from opskit.api.routers import leads as route
from opskit.api.routers import runs as runs_route
from opskit.db.migrations.versions import core_0010_column_bounds, crm_0003_bounds
from opskit.leads.models import ResearchOutcome, empty_fields
from opskit.leads.retrieval import MAX_PAGE_URL_CHARS


class _Orig(Exception):
    def __init__(self, sqlstate: str, constraint: str | None = None) -> None:
        super().__init__("refused")
        self.sqlstate = sqlstate
        self.diag = SimpleNamespace(
            constraint_name=constraint, message_primary="research.fields: too large"
        )


class _Session:
    @asynccontextmanager
    async def begin(self) -> Any:
        yield self


class _Factory:
    def __init__(self) -> None:
        self.session = _Session()

    @asynccontextmanager
    async def __call__(self) -> Any:
        yield self.session


def _outcome() -> ResearchOutcome:
    return ResearchOutcome(
        company_name="Brightwell Example Co",
        city_hint="",
        website="brightwell.example",
        domain="brightwell.example",
        status="researched",
        fields=empty_fields(),
        replay_key="c" * 64,
        cost_usd="0.003",
        latency_ms=42,
    )


def _wire(monkeypatch: pytest.MonkeyPatch, *, refuse: IntegrityError | None) -> dict[str, Any]:
    seen: dict[str, Any] = {"sessions": [], "failures": []}
    factory = _Factory()

    async def run_context(*_: Any) -> Any:
        return object()

    async def no_stored(*_: Any, **__: Any) -> None:
        return None

    async def research_company(*_: Any, **__: Any) -> ResearchOutcome:
        return _outcome()

    async def upsert(session: Any, outcome: Any) -> Any:
        seen["sessions"].append(session)
        return SimpleNamespace(action="created", account_id=uuid4())

    async def save(session: Any, *_: Any, **__: Any) -> None:
        seen["sessions"].append(session)
        if refuse is not None:
            raise refuse

    async def record_failure(_: Any, __: Any, reason: str, *, trace: Any = None) -> None:
        seen["failures"].append((reason, trace))

    monkeypatch.setattr(route, "_database", lambda request: factory)
    monkeypatch.setattr(route, "_run_context", run_context)
    monkeypatch.setattr(route, "_retriever", lambda *_: object())
    monkeypatch.setattr(route, "_core", lambda request: SimpleNamespace(models=object()))
    monkeypatch.setattr(route, "research_company", research_company)
    monkeypatch.setattr(route, "_record_failure", record_failure)
    monkeypatch.setattr(route.store, "load_research", no_stored)
    monkeypatch.setattr(route.store, "upsert_account_in", upsert)
    monkeypatch.setattr(route.store, "save_research_in", save)
    seen["factory"] = factory
    return seen


def _request() -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=None)))


def _body() -> route.ResearchRequest:
    return route.ResearchRequest(
        run_id=uuid4(), company_name="Brightwell Example Co", website="brightwell.example"
    )


async def test_both_writes_share_one_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _wire(monkeypatch, refuse=None)
    result = await route.research(_request(), _body())
    assert result.crm_action == "created"
    assert seen["sessions"] == [seen["factory"].session, seen["factory"].session]
    assert seen["failures"] == []


async def test_a_bound_refusal_is_a_recorded_failure_with_the_cost_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refusal = IntegrityError("insert", {}, _Orig("23514"))
    seen = _wire(monkeypatch, refuse=refusal)
    with pytest.raises(HTTPException) as caught:
        await route.research(_request(), _body())
    assert caught.value.status_code == 422
    ((reason, trace),) = seen["failures"]
    assert reason.endswith("result too large to store")
    assert trace.replay_key == "c" * 64 and trace.cost_usd == "0.003"


@pytest.mark.parametrize(
    "orig",
    [_Orig("23514", constraint="research_status_check"), _Orig("23505"), _Orig("23503")],
    ids=["native-check-constraint", "unique-violation", "foreign-key"],
)
async def test_any_other_integrity_error_still_surfaces(
    monkeypatch: pytest.MonkeyPatch, orig: _Orig
) -> None:
    seen = _wire(monkeypatch, refuse=IntegrityError("insert", {}, orig))
    with pytest.raises(IntegrityError):
        await route.research(_request(), _body())
    assert seen["failures"] == []


def test_the_page_url_cap_matches_the_crm_source_column() -> None:
    assert crm_0003_bounds.ACCOUNT_SOURCES["source_ref"]["max"] == MAX_PAGE_URL_CHARS


def test_the_n8n_id_pattern_matches_the_database_and_refuses_what_it_refuses() -> None:
    assert runs_route.N8N_ID_PATTERN == core_0010_column_bounds.N8N_ID
    for ok in ("", "123", "leads00000000001", "a.b_c:d/e-f", "x" * 64):
        runs_route.StartRun(workflow="kit_smoke", n8n_execution_id=ok)
    for bad in ("a b", "abc\n", "_a", "a@b", "x" * 65):
        with pytest.raises(ValidationError):
            runs_route.StartRun(workflow="kit_smoke", n8n_execution_id=bad)
