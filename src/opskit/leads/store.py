"""Persistence for company research and the CRM upsert it feeds (schemas `leads` and `crm`)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import func, literal_column, select
from sqlalchemy.dialects.postgresql import insert

from opskit.db.engine import SessionFactory
from opskit.db.tables import crm_account_sources as sources_table
from opskit.db.tables import crm_accounts as accounts
from opskit.db.tables import leads_research as research
from opskit.leads.models import FIELDS, FieldValue, Finding, ResearchOutcome

NO_WEBSITE_REASON = "no website given"

# The accounts columns research fills in. `domain` is the upsert key, so it is not one of them.
ACCOUNT_FIELDS = tuple(name for name in FIELDS if name != "domain")


class StoredResearch(BaseModel):
    outcome: ResearchOutcome
    crm_action: Literal["created", "updated"] | None
    account_id: UUID | None


class UpsertResult(BaseModel):
    account_id: UUID
    action: Literal["created", "updated"]
    sources_written: int


class UnresolvedCompany(BaseModel):
    company: str
    reason: str


class NullFields(BaseModel):
    company: str
    fields: list[str]


class FieldConflict(BaseModel):
    company: str
    field: str


class LeadsSummary(BaseModel):
    researched: int
    created: list[str]
    updated: list[str]
    no_website: list[str]
    unresolved: list[UnresolvedCompany]
    null_fields: list[NullFields]
    conflicts: list[FieldConflict]
    cost_usd: str


# --- research -----------------------------------------------------------------------------


async def save_research(
    session_factory: SessionFactory,
    run_id: UUID,
    outcome: ResearchOutcome,
    *,
    crm_action: str | None,
    account_id: UUID | None,
) -> None:
    """Store a company's result; researching it again in the same run overwrites it."""
    values: dict[str, Any] = {
        "company_name": outcome.company_name,
        "city_hint": outcome.city_hint,
        "website": outcome.website,
        "domain": outcome.domain,
        "status": outcome.status,
        "reason": outcome.reason,
        "fields": {
            name: value.model_dump() if value is not None else None
            for name, value in outcome.fields.items()
        },
        "findings": [finding.model_dump() for finding in outcome.findings],
        "pages": outcome.pages,
        "raw_cites": outcome.raw_cites,
        "valid_cites": outcome.valid_cites,
        "replay_key": outcome.replay_key,
        "cost_usd": Decimal(outcome.cost_usd),
        "latency_ms": outcome.latency_ms,
        "crm_action": crm_action,
        "account_id": account_id,
    }
    statement = insert(research).values(run_id=run_id, **values)
    statement = statement.on_conflict_do_update(
        index_elements=[research.c.run_id, research.c.company_name, research.c.city_hint],
        set_={
            name: statement.excluded[name]
            for name in values
            if name != "company_name" and name != "city_hint"
        },
    )
    async with session_factory() as session, session.begin():
        await session.execute(statement)


async def load_research(
    session_factory: SessionFactory, run_id: UUID, company_name: str, city_hint: str
) -> StoredResearch | None:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(research).where(
                    research.c.run_id == run_id,
                    research.c.company_name == company_name,
                    research.c.city_hint == city_hint,
                )
            )
        ).first()
    return _research_of(row) if row is not None else None


def _research_of(row: Any) -> StoredResearch:
    outcome = ResearchOutcome(
        company_name=row.company_name,
        city_hint=row.city_hint,
        website=row.website,
        domain=row.domain,
        status=row.status,
        reason=row.reason,
        fields={
            name: FieldValue.model_validate(value) if value is not None else None
            for name, value in row.fields.items()
        },
        findings=[Finding.model_validate(f) for f in row.findings],
        pages=row.pages,
        raw_cites=row.raw_cites,
        valid_cites=row.valid_cites,
        replay_key=row.replay_key.strip() if row.replay_key else None,
        cost_usd=format(row.cost_usd, "f"),
        latency_ms=row.latency_ms,
    )
    return StoredResearch(outcome=outcome, crm_action=row.crm_action, account_id=row.account_id)


# --- CRM ----------------------------------------------------------------------------------


def _account_value(name: str, field: FieldValue | None) -> str | int | None:
    if field is None:
        return None
    return int(field.value) if name == "founded_year" else str(field.value)


async def upsert_account(
    session_factory: SessionFactory, outcome: ResearchOutcome
) -> UpsertResult | None:
    """Create or enrich the CRM account for a researched company, with a source per field.

    Keyed on the domain. A field the research did not verify never blanks a value the
    account already has, and never touches that field's existing source row.
    """
    if outcome.domain is None or outcome.status != "researched":
        return None
    researched = {name: _account_value(name, outcome.fields.get(name)) for name in ACCOUNT_FIELDS}
    new_account = insert(accounts).values(
        name=outcome.company_name, domain=outcome.domain, **researched
    )
    account = new_account.on_conflict_do_update(
        index_elements=[accounts.c.domain],
        set_={
            **{
                name: func.coalesce(new_account.excluded[name], accounts.c[name])
                for name in researched
            },
            "updated_at": func.now(),
        },
    ).returning(accounts.c.id, literal_column("(xmax = 0)").label("inserted"))
    found = {name: value for name, value in outcome.fields.items() if value is not None}
    async with session_factory() as session, session.begin():
        row = (await session.execute(account)).one()
        if found:
            source_rows = [
                {
                    "account_id": row.id,
                    "field": name,
                    "source_ref": value.source_url,
                    "excerpt": (
                        f"Derived from the website given: {value.source_url}"
                        if value.derived
                        else value.quote
                    ),
                }
                for name, value in found.items()
            ]
            sources = insert(sources_table).values(source_rows)
            await session.execute(
                sources.on_conflict_do_update(
                    index_elements=[sources_table.c.account_id, sources_table.c.field],
                    set_={
                        "source_ref": sources.excluded.source_ref,
                        "excerpt": sources.excluded.excerpt,
                        "found_at": func.now(),
                    },
                )
            )
    return UpsertResult(
        account_id=row.id,
        action="created" if row.inserted else "updated",
        sources_written=len(found),
    )


# --- summary ------------------------------------------------------------------------------


async def summary(session_factory: SessionFactory, run_id: UUID) -> LeadsSummary:
    """What a run did, grouped for the report; every list is sorted so output is stable."""
    async with session_factory() as session:
        rows = (await session.execute(select(research).where(research.c.run_id == run_id))).all()
    stored = sorted((_research_of(row) for row in rows), key=lambda s: s.outcome.company_name)
    done = [s for s in stored if s.outcome.status == "researched"]
    unresolved = [s.outcome for s in stored if s.outcome.status == "unresolved"]
    return LeadsSummary(
        researched=len(done),
        created=[s.outcome.company_name for s in done if s.crm_action == "created"],
        updated=[s.outcome.company_name for s in done if s.crm_action == "updated"],
        no_website=[o.company_name for o in unresolved if o.reason == NO_WEBSITE_REASON],
        unresolved=[
            UnresolvedCompany(company=o.company_name, reason=o.reason or "")
            for o in unresolved
            if o.reason != NO_WEBSITE_REASON
        ],
        null_fields=[
            NullFields(company=s.outcome.company_name, fields=missing)
            for s in done
            if (missing := [n for n in FIELDS if s.outcome.fields.get(n) is None])
        ],
        conflicts=sorted(
            (
                FieldConflict(company=s.outcome.company_name, field=finding.field)
                for s in stored
                for finding in s.outcome.findings
                if finding.kind == "conflict"
            ),
            key=lambda c: (c.company, c.field),
        ),
        cost_usd=format(sum((Decimal(s.outcome.cost_usd) for s in stored), Decimal(0)), "f"),
    )
