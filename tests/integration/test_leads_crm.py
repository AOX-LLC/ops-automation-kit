"""The leads store against the running stack: CRM upsert, sources, grants and the run summary.

The store is exercised through the real code inside the api container (as the app role);
the database is read back with psql, so these tests see what a second connection sees.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from tests.integration.conftest import compose, psql

pytestmark = pytest.mark.integration

# Runs one scenario in the api container. argv[1] is a JSON list of steps; each step is
# {"op": "upsert"|"save"|"summary", "outcome": {...}, "run_id": "..."}; results are printed as JSON.
_DRIVER = """
import asyncio, json, sys
from uuid import UUID
from opskit.config import Settings
from opskit.db.engine import make_engine, make_session_factory
from opskit.leads import store
from opskit.leads.models import ResearchOutcome

async def main(steps):
    engine = make_engine(Settings())
    factory = make_session_factory(engine)
    results = []
    for step in steps:
        outcome = ResearchOutcome.model_validate(step["outcome"]) if "outcome" in step else None
        if step["op"] == "upsert":
            result = await store.upsert_account(factory, outcome)
            results.append(result.model_dump(mode="json") if result else None)
        elif step["op"] == "save":
            await store.save_research(
                factory, UUID(step["run_id"]), outcome,
                crm_action=step.get("crm_action"),
                account_id=UUID(step["account_id"]) if step.get("account_id") else None,
            )
            loaded = await store.load_research(
                factory, UUID(step["run_id"]), outcome.company_name, outcome.city_hint
            )
            results.append(loaded.model_dump(mode="json"))
        else:
            summary = await store.summary(factory, UUID(step["run_id"]))
            results.append(summary.model_dump(mode="json"))
    await engine.dispose()
    print(json.dumps(results))

asyncio.run(main(json.loads(sys.argv[1])))
"""


def _drive(*steps: dict[str, Any]) -> list[Any]:
    result = compose(
        "exec", "-T", "api", "python", "-c", _DRIVER, json.dumps(list(steps)), check=False
    )
    assert result.returncode == 0, result.stderr
    results: list[Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return results


def _sql(query: str) -> str:
    result = psql(query)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _field(value: str | int, domain: str, quote: str = "quote") -> dict[str, Any]:
    return {"value": value, "source_url": f"https://{domain}/about", "quote": quote}


def _outcome(site: str, **overrides: Any) -> dict[str, Any]:
    """A fully researched, fictional company on a `.example` domain."""
    base: dict[str, Any] = {
        "company_name": f"Company {site}",
        "city_hint": "Springfield",
        "website": f"https://{site}",
        "domain": site,
        "status": "researched",
        "fields": {
            "domain": _field(site, site, "Welcome to us"),
            "industry": _field("Plumbing", site, "We fix pipes"),
            "employee_band": _field("11-50", site, "A team of 20"),
            "hq_city": _field("Springfield", site, "Based in Springfield"),
            "founded_year": _field(1999, site, "Since 1999"),
            "description": _field("Local plumbers", site, "Local plumbers"),
        },
    }
    return {**base, **overrides}


@pytest.fixture
def domain() -> str:
    return f"it-{uuid4().hex[:12]}.example"


@pytest.fixture
def run_id(service: httpx.Client) -> str:
    response = service.post(
        "/v1/runs", json={"workflow": "leads", "n8n_execution_id": f"itest-{uuid4()}"}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["run_id"])


def test_upsert_twice_creates_then_updates_one_account(domain: str) -> None:
    first, second = _drive(
        {"op": "upsert", "outcome": _outcome(domain)},
        {"op": "upsert", "outcome": _outcome(domain)},
    )
    assert first["action"] == "created"
    assert second["action"] == "updated"
    assert first["account_id"] == second["account_id"]
    assert first["sources_written"] == 6
    assert _sql(f"select count(*) from crm.accounts where domain = '{domain}'") == "1"
    assert _sql(f"select founded_year from crm.accounts where domain = '{domain}'") == "1999"


def test_sources_are_one_row_per_field_and_rerun_updates(domain: str) -> None:
    changed = _outcome(domain)
    changed["fields"]["industry"] = _field("Plumbing", domain, "A changed quote")
    created, _ = _drive(
        {"op": "upsert", "outcome": _outcome(domain)},
        {"op": "upsert", "outcome": changed},
    )
    account_id = created["account_id"]
    assert (
        _sql(f"select count(*) from crm.account_sources where account_id = '{account_id}'") == "6"
    )
    assert (
        _sql(
            f"select excerpt from crm.account_sources "
            f"where account_id = '{account_id}' and field = 'industry'"
        )
        == "A changed quote"
    )


def test_a_null_field_never_overwrites_or_removes_a_source(domain: str) -> None:
    sparse = _outcome(domain)
    sparse["fields"]["industry"] = None
    sparse["fields"]["description"] = _field("A new description", domain, "New words")
    created, again = _drive(
        {"op": "upsert", "outcome": _outcome(domain)},
        {"op": "upsert", "outcome": sparse},
    )
    assert again["action"] == "updated"
    assert again["sources_written"] == 5
    assert _sql(f"select industry from crm.accounts where domain = '{domain}'") == "Plumbing"
    assert _sql(f"select description from crm.accounts where domain = '{domain}'") == (
        "A new description"
    )
    assert (
        _sql(
            f"select excerpt from crm.account_sources "
            f"where account_id = '{created['account_id']}' and field = 'industry'"
        )
        == "We fix pipes"
    )


def test_the_account_name_is_never_changed_by_a_later_run(domain: str) -> None:
    _drive(
        {"op": "upsert", "outcome": _outcome(domain, company_name="First Name")},
        {"op": "upsert", "outcome": _outcome(domain, company_name="Second Name")},
    )
    assert _sql(f"select name from crm.accounts where domain = '{domain}'") == "First Name"


def test_unresolved_outcome_writes_no_account(domain: str) -> None:
    unresolved = _outcome(
        domain,
        status="unresolved",
        reason="site unreachable",
        fields={name: None for name in _outcome(domain)["fields"]},
    )
    no_domain = _outcome(domain, domain=None)
    assert _drive(
        {"op": "upsert", "outcome": unresolved}, {"op": "upsert", "outcome": no_domain}
    ) == [None, None]
    assert _sql(f"select count(*) from crm.accounts where domain = '{domain}'") == "0"


@pytest.mark.parametrize(
    "sql",
    [
        "begin; delete from crm.account_sources; rollback;",
        "begin; delete from leads.research; rollback;",
    ],
)
def test_app_role_cannot_delete_sources_or_research(sql: str) -> None:
    result = psql(sql, role="opskit_app")
    assert result.returncode != 0
    assert "permission denied" in result.stderr


@pytest.mark.parametrize(
    "sql",
    [
        "begin; update crm.account_sources set account_id = account_id; rollback;",
        "begin; update crm.account_sources set field = field; rollback;",
        "begin; update leads.research set company_name = company_name; rollback;",
        "begin; update leads.research set run_id = run_id; rollback;",
    ],
)
def test_app_role_cannot_change_what_identifies_a_source_or_research_row(sql: str) -> None:
    result = psql(sql, role="opskit_app")
    assert result.returncode != 0
    assert "permission denied" in result.stderr


def test_research_is_overwritten_when_the_same_company_is_rerun(run_id: str, domain: str) -> None:
    outcome = _outcome(domain)
    first, second = _drive(
        {"op": "save", "run_id": run_id, "outcome": outcome, "crm_action": "created"},
        {"op": "save", "run_id": run_id, "outcome": {**outcome, "latency_ms": 7}},
    )
    assert first["crm_action"] == "created"
    assert second["crm_action"] is None
    assert second["outcome"]["latency_ms"] == 7
    assert second["outcome"]["fields"]["founded_year"]["value"] == 1999
    assert _sql(f"select count(*) from leads.research where run_id = '{run_id}'") == "1"


def test_summary_groups_a_run(run_id: str, domain: str) -> None:
    gaps = _outcome(f"b-{domain}", company_name="Bravo")
    gaps["fields"]["hq_city"] = None
    gaps["fields"]["founded_year"] = None
    gaps["findings"] = [{"field": "industry", "kind": "conflict", "detail": "two answers"}]
    blank = {name: None for name in gaps["fields"]}
    steps: list[dict[str, Any]] = [
        {
            "op": "save",
            "run_id": run_id,
            "crm_action": "created",
            "outcome": _outcome(f"a-{domain}", company_name="Alpha", cost_usd="0.010000"),
        },
        {
            "op": "save",
            "run_id": run_id,
            "crm_action": "updated",
            "outcome": {**gaps, "cost_usd": "0.020000"},
        },
        {
            "op": "save",
            "run_id": run_id,
            "outcome": _outcome(
                domain,
                company_name="Delta",
                domain=None,
                website=None,
                status="unresolved",
                reason="no website given",
                fields=blank,
            ),
        },
        {
            "op": "save",
            "run_id": run_id,
            "outcome": _outcome(
                domain,
                company_name="Charlie",
                status="unresolved",
                reason="site unreachable",
                fields=blank,
            ),
        },
        {"op": "summary", "run_id": run_id},
    ]
    summary = _drive(*steps)[-1]
    assert summary["researched"] == 2
    assert summary["created"] == ["Alpha"]
    assert summary["updated"] == ["Bravo"]
    assert summary["no_website"] == ["Delta"]
    assert summary["unresolved"] == [{"company": "Charlie", "reason": "site unreachable"}]
    assert summary["null_fields"] == [{"company": "Bravo", "fields": ["hq_city", "founded_year"]}]
    assert summary["conflicts"] == [{"company": "Bravo", "field": "industry"}]
    assert summary["cost_usd"] == "0.030000"
