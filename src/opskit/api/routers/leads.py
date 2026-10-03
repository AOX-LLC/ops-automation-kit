"""Leads for n8n: research one company and write its CRM record, then summarise the run."""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy.exc import IntegrityError

from opskit.api.auth import ServiceAuth
from opskit.api.routers.inputs import read_companies
from opskit.config import Settings
from opskit.core.errors import ModelRefusalError, NotFound, ReplayMissError, StructuredOutputError
from opskit.core.ports import Core, RunContext
from opskit.db.engine import SessionFactory
from opskit.leads import store
from opskit.leads.extraction import research_company
from opskit.leads.models import ResearchOutcome, empty_fields
from opskit.leads.netguard import GuardedFetcher
from opskit.leads.retrieval import (
    Company,
    CorpusRetriever,
    Retriever,
    WebRetriever,
    normalize_website,
)
from opskit.leads.robots import RobotsCache
from opskit.receipts.store import session_factory_of

log = logging.getLogger(__name__)
BOUND_VIOLATION = "23514"  # check_violation, which core.enforce_bounds() raises


def _is_bound_refusal(exc: IntegrityError) -> bool:
    """A refusal by core.enforce_bounds(): a check_violation that names no constraint (a native
    CHECK constraint does, and that is a bug to surface, not a size to report)."""
    orig = exc.orig
    diag = getattr(orig, "diag", None)
    return (
        getattr(orig, "sqlstate", None) == BOUND_VIOLATION
        and getattr(diag, "constraint_name", None) is None
    )


def _bound_message(exc: IntegrityError) -> str:
    """The refusal's own text, `table.column: problem`: it names no value."""
    return str(getattr(getattr(exc.orig, "diag", None), "message_primary", "unknown"))


router = APIRouter(prefix="/v1/leads", tags=["leads"], dependencies=[ServiceAuth])

FAILED = "research failed"  # reason on a row saved when the model or replay failed
RUNS_KEPT = 8  # robots decisions are cached per run; older runs' caches are dropped


class ResearchRequest(BaseModel):
    run_id: UUID
    company_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
    ]
    city_hint: str = Field(default="", max_length=100)
    website: str | None = Field(default=None, max_length=253)


class ResearchResult(BaseModel):
    outcome: ResearchOutcome
    crm_action: str | None = None
    account_id: UUID | None = None


def _database(request: Request) -> SessionFactory:
    factory = session_factory_of(request.app)
    if factory is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable")
    return factory


def _core(request: Request) -> Core:
    core: Core = request.app.state.core
    return core


async def _run_context(request: Request, run_id: UUID) -> RunContext:
    try:
        return await _core(request).runs.get(run_id)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found") from exc


class WebSession:
    """One guarded fetcher for the process, and one robots.txt cache per run."""

    def __init__(self) -> None:
        self.fetcher = GuardedFetcher()
        self._robots: OrderedDict[UUID, RobotsCache] = OrderedDict()

    def retriever(self, run_id: UUID) -> WebRetriever:
        cache = self._robots.get(run_id)
        if cache is None:
            cache = self._robots[run_id] = RobotsCache(self.fetcher)
            while len(self._robots) > RUNS_KEPT:
                self._robots.popitem(last=False)
        return WebRetriever(self.fetcher, cache)

    async def aclose(self) -> None:
        await self.fetcher.aclose()


def _retriever(request: Request, run_id: UUID) -> Retriever:
    settings: Settings = request.app.state.settings
    if settings.leads_retrieval == "web":
        session: WebSession = request.app.state.leads_web  # made at startup in web mode
        return session.retriever(run_id)
    leads_dir = settings.samples_dir / "leads"
    domains = frozenset(
        item.website for item in read_companies(leads_dir / "companies.csv") if item.website
    )
    return CorpusRetriever(leads_dir / "corpus", domains)


async def _record_failure(
    factory: SessionFactory,
    body: ResearchRequest,
    reason: str,
    *,
    trace: ResearchOutcome | None = None,
) -> None:
    """Keep a company whose research failed in the run, so the summary lists it. `trace` is the
    outcome of a model call that did run, so its cost and replay key are not lost."""
    outcome = ResearchOutcome(
        company_name=body.company_name,
        city_hint=body.city_hint,
        website=body.website,
        domain=normalize_website(body.website),
        status="unresolved",
        reason=reason,
        fields=empty_fields(),
        replay_key=trace.replay_key if trace else None,
        cost_usd=trace.cost_usd if trace else "0",
        latency_ms=trace.latency_ms if trace else None,
    )
    await store.save_research(factory, body.run_id, outcome, crm_action=None, account_id=None)


@router.post("/research")
async def research(request: Request, body: ResearchRequest) -> ResearchResult:
    """Research one company and upsert its CRM record; a repeat returns the stored result."""
    factory = _database(request)
    ctx = await _run_context(request, body.run_id)
    stored = await store.load_research(factory, body.run_id, body.company_name, body.city_hint)
    if stored is not None and not (stored.outcome.reason or "").startswith(FAILED):
        return ResearchResult(
            outcome=stored.outcome, crm_action=stored.crm_action, account_id=stored.account_id
        )
    company = Company(body.company_name, body.city_hint, body.website)
    try:
        outcome = await research_company(
            _core(request).models, ctx, _retriever(request, body.run_id), company
        )
    except ReplayMissError as exc:
        await _record_failure(factory, body, f"{FAILED}: no recording")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"no recording for replay key {exc.key or 'unknown'}",
        ) from exc
    except (ModelRefusalError, StructuredOutputError) as exc:
        await _record_failure(factory, body, f"{FAILED}: {type(exc).__name__}")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"research failed: {type(exc).__name__}"
        ) from exc
    try:
        # One transaction: a refusal from either write rolls back both, so the CRM never holds
        # an account the run does not record.
        async with factory() as session, session.begin():
            upserted = await store.upsert_account_in(session, outcome)
            action = upserted.action if upserted else None
            account_id = upserted.account_id if upserted else None
            await store.save_research_in(
                session, body.run_id, outcome, crm_action=action, account_id=account_id
            )
    except IntegrityError as exc:
        if not _is_bound_refusal(exc):
            raise
        # What a website made the model quote does not fit a stored column (the database bounds
        # every one). The company stays in the run, listed as failed, instead of dropping out.
        log.warning("research result refused by a database bound: %s", _bound_message(exc))
        await _record_failure(factory, body, f"{FAILED}: result too large to store", trace=outcome)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "research result too large to store"
        ) from exc
    return ResearchResult(outcome=outcome, crm_action=action, account_id=account_id)


@router.get("/summary")
async def run_summary(request: Request, run_id: UUID) -> store.LeadsSummary:
    return await store.summary(_database(request), run_id)
