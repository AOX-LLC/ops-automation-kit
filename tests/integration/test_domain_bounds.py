"""Phase 3d: the domain tables refuse what the application would never write.

Each case goes to the database directly as `opskit_app` (the role the API runs as), so it holds
even if the application code is wrong or bypassed. A case runs one insert inside a DO block that
always ends in an exception, so nothing is kept: a bound that fires shows its own message, an
accepted row shows 'inserted ok'. Parent rows (run, account, message) are made inside the same
block and roll back with it.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tests.integration.conftest import psql

pytestmark = pytest.mark.integration

ACCEPTED = "inserted ok"
PARENT_MESSAGE = "parent@example.test"
HEX64 = "repeat('a', 64)"

# JSON values, as SQL.
NESTED = "(repeat('[', 20) || repeat(']', 20))::jsonb"
NESTED_OBJECT = f"jsonb_build_object('a', {NESTED})"


def long_text(length: int) -> str:
    return f"repeat('x', {length})"


def big_object(length: int) -> str:
    return f"jsonb_build_object('a', repeat('x', {length}))"


def items(count: int) -> str:
    return f"(select jsonb_agg(1) from generate_series(1, {count}))"


# A fully legitimate row per table: column -> SQL. `r`, `a` are the parent run and account.
BASE: dict[str, dict[str, str]] = {
    "crm.accounts": {
        "name": "'Acme Plumbing'",
        "domain": "'acme-plumbing.example'",
        "industry": "'Plumbing'",
        "employee_band": "'11-50'",
        "hq_city": "'Springfield'",
        "description": "'Local plumber.'",
        "founded_year": "1998",
    },
    "crm.account_sources": {
        "account_id": "a",
        "field": "'industry'",
        "source_ref": "'https://acme-plumbing.example/about'",
        "excerpt": "'Plumbing since 1998.'",
    },
    "receipts.extractions": {
        "path": "'receipts/lunch.png'",
        "sha256": HEX64,
        "run_id": "r",
        "status": "'extracted'",
        "reason": "'ok'",
        "fields": "'{\"total\": 12.5}'::jsonb",
        "replay_key": HEX64,
        "tier": "'small'",
        "model": "'model-x'",
        "cost_usd": "0.01",
        "latency_ms": "120",
    },
    "receipts.reconciliations": {
        "run_id": "r",
        "rows": '\'[{"path": "receipts/lunch.png"}]\'::jsonb',
        "summary": "'{\"matched\": 1}'::jsonb",
    },
    "leads.research": {
        "run_id": "r",
        "company_name": "'Acme Plumbing'",
        "city_hint": "'Springfield'",
        "website": "'https://acme-plumbing.example'",
        "domain": "'acme-plumbing.example'",
        "status": "'researched'",
        "reason": "'found'",
        "fields": "'{}'::jsonb",
        "findings": "'[]'::jsonb",
        "pages": "'[]'::jsonb",
        "raw_cites": "3",
        "valid_cites": "2",
        "replay_key": HEX64,
        "cost_usd": "0.02",
        "latency_ms": "900",
    },
    "inbox.messages": {
        "message_id": "'child@example.test'",
        "mailpit_id": "'mp-1'",
        "from_header": "'Pat Example <pat@example.test>'",
        "reply_to_header": "'pat@example.test'",
        "to_addr": "'help@example.test'",
        "subject": "'Opening hours'",
        "received_at": "'2026-10-01T09:00:00Z'",
        "body_text": "'When are you open?'",
    },
    "inbox.triage": {
        "message_id": f"'{PARENT_MESSAGE}'",
        "run_id": "r",
        "category": "'support'",
        "priority": "'normal'",
        "needs_reply": "true",
        "escalate": "false",
        "route": "'draft'",
        "quarantined": "false",
        "injection_reasons": "'[]'::jsonb",
        "replay_key": HEX64,
        "cost_usd": "0.01",
        "latency_ms": "300",
    },
    "inbox.drafts": {
        "message_id": f"'{PARENT_MESSAGE}'",
        "run_id": "r",
        "to_addr": "'pat@example.test'",
        "subject": "'Re: Opening hours'",
        "in_reply_to": "'<abc@example.test>'",
        "body": "'We open at nine.'",
        "facts_used": "'[\"hours\"]'::jsonb",
        "grounding": '\'{"hours": "9-5"}\'::jsonb',
        "status": "'draft'",
        "failure_reason": "'none'",
        "replay_key": HEX64,
        "cost_usd": "0.01",
        "latency_ms": "400",
        "sent_at": "now()",
    },
}


def run_insert(table: str, **overrides: str) -> str:
    """Insert the table's legitimate row with `overrides` (column -> SQL) and report the outcome:
    the database's error text if a bound refused it, or 'inserted ok'. Nothing is kept."""
    row = {**BASE[table], **overrides}
    columns = ", ".join(row)
    values = ", ".join(row.values())
    result = psql(
        f"""
        do $$
        declare
            r uuid;
            a uuid;
        begin
            insert into core.runs (workflow, mode) values ('inbox', 'replay') returning id into r;
            insert into crm.accounts (name) values ('Parent Co') returning id into a;
            insert into inbox.messages (message_id, mailpit_id, from_header, subject, body_text)
                values ('{PARENT_MESSAGE}', 'mp-0', 'x@example.test', '', '');
            insert into {table} ({columns}) values ({values});
            raise exception '{ACCEPTED}';
        end $$
        """,
        role="opskit_app",
    )
    assert result.returncode != 0
    return result.stderr


def assert_refused(table: str, column: str, problem: str, **override: str) -> None:
    stderr = run_insert(table, **override)
    name = table.split(".")[1]
    assert f"{name}.{column}: {problem}" in stderr, stderr


# --- the legitimate rows, and the values the application writes on purpose ---------------------

ALLOWED: list[tuple[str, dict[str, str]]] = [
    ("crm.accounts", {}),
    (
        "crm.accounts",
        {
            "domain": "null",
            "industry": "null",
            "employee_band": "null",
            "hq_city": "null",
            "description": "null",
            "founded_year": "null",
        },
    ),
    ("crm.account_sources", {}),
    ("receipts.extractions", {}),
    (
        "receipts.extractions",
        {  # an unreadable file
            "sha256": "''",
            "status": "'failed'",
            "fields": "null",
            "reason": "null",
            "replay_key": "null",
            "tier": "null",
            "model": "null",
            "latency_ms": "null",
        },
    ),
    ("receipts.reconciliations", {}),
    ("leads.research", {}),
    (
        "leads.research",
        {  # no city, nothing found
            "city_hint": "''",
            "website": "null",
            "domain": "null",
            "reason": "null",
            "status": "'unresolved'",
            "replay_key": "null",
            "latency_ms": "null",
        },
    ),
    ("inbox.messages", {}),
    (
        "inbox.messages",
        {
            "reply_to_header": "null",
            "to_addr": "null",
            "subject": "''",
            "received_at": "null",
            "body_text": "''",
        },
    ),
    ("inbox.triage", {}),
    ("inbox.drafts", {}),
    (
        "inbox.drafts",
        {  # a failed draft
            "to_addr": "''",
            "subject": "''",
            "body": "''",
            "status": "'failed'",
            "failure_reason": "'model refused'",
            "in_reply_to": "null",
            "replay_key": "null",
            "latency_ms": "null",
            "sent_at": "null",
        },
    ),
]


@pytest.mark.parametrize(
    ("table", "overrides"), ALLOWED, ids=[f"{t}-{i}" for i, (t, _) in enumerate(ALLOWED)]
)
def test_a_legitimate_row_is_accepted(table: str, overrides: dict[str, str]) -> None:
    stderr = run_insert(table, **overrides)
    assert ACCEPTED in stderr, stderr
    assert "check_violation" not in stderr


# --- refusals: (table, column, SQL value, the problem the database names) ---------------------

TOO_LONG = "too long"
BAD_ENUM = "not an allowed value"
RANGE = "out of range"
EMPTY = "too short"
FORM = "not in the expected form"

REFUSALS: list[tuple[str, str, str, str]] = [
    # crm.accounts
    ("crm.accounts", "name", long_text(201), TOO_LONG),
    ("crm.accounts", "name", "''", EMPTY),
    ("crm.accounts", "domain", long_text(254), TOO_LONG),
    ("crm.accounts", "industry", long_text(201), TOO_LONG),
    ("crm.accounts", "employee_band", long_text(33), TOO_LONG),
    ("crm.accounts", "hq_city", long_text(201), TOO_LONG),
    ("crm.accounts", "description", long_text(1001), TOO_LONG),
    ("crm.accounts", "founded_year", "1799", RANGE),
    ("crm.accounts", "founded_year", "2101", RANGE),
    # crm.account_sources
    ("crm.account_sources", "field", "'website'", BAD_ENUM),
    ("crm.account_sources", "source_ref", long_text(513), TOO_LONG),
    ("crm.account_sources", "source_ref", "''", EMPTY),
    ("crm.account_sources", "excerpt", long_text(1001), TOO_LONG),
    ("crm.account_sources", "excerpt", "''", EMPTY),
    # receipts.extractions
    ("receipts.extractions", "path", long_text(513), TOO_LONG),
    ("receipts.extractions", "path", "''", EMPTY),
    ("receipts.extractions", "sha256", long_text(65), TOO_LONG),
    ("receipts.extractions", "sha256", "repeat('A', 64)", FORM),
    ("receipts.extractions", "sha256", "'abc'", FORM),
    ("receipts.extractions", "reason", long_text(201), TOO_LONG),
    ("receipts.extractions", "fields", "'[]'::jsonb", "must be a JSON object"),
    ("receipts.extractions", "fields", big_object(65536), "too large"),
    ("receipts.extractions", "fields", NESTED_OBJECT, "nested too deeply"),
    ("receipts.extractions", "tier", long_text(101), TOO_LONG),
    ("receipts.extractions", "model", long_text(101), TOO_LONG),
    ("receipts.extractions", "replay_key", "repeat('A', 64)", FORM),
    ("receipts.extractions", "cost_usd", "-1", RANGE),
    ("receipts.extractions", "latency_ms", "100000001", RANGE),
    ("receipts.extractions", "latency_ms", "-1", RANGE),
    # receipts.reconciliations
    ("receipts.reconciliations", "rows", "'{}'::jsonb", "must be a JSON array"),
    ("receipts.reconciliations", "rows", f"jsonb_build_array({long_text(4_194_304)})", "too large"),
    ("receipts.reconciliations", "rows", NESTED, "nested too deeply"),
    ("receipts.reconciliations", "summary", "'[]'::jsonb", "must be a JSON object"),
    ("receipts.reconciliations", "summary", big_object(4096), "too large"),
    ("receipts.reconciliations", "summary", NESTED_OBJECT, "nested too deeply"),
    # leads.research
    ("leads.research", "company_name", long_text(201), TOO_LONG),
    ("leads.research", "company_name", "''", EMPTY),
    ("leads.research", "city_hint", long_text(101), TOO_LONG),
    ("leads.research", "website", long_text(254), TOO_LONG),
    ("leads.research", "domain", long_text(254), TOO_LONG),
    ("leads.research", "reason", long_text(301), TOO_LONG),
    ("leads.research", "fields", "'[]'::jsonb", "must be a JSON object"),
    ("leads.research", "fields", big_object(16384), "too large"),
    ("leads.research", "fields", NESTED_OBJECT, "nested too deeply"),
    ("leads.research", "findings", "'{}'::jsonb", "must be a JSON array"),
    ("leads.research", "findings", f"jsonb_build_array({long_text(65536)})", "too large"),
    ("leads.research", "findings", NESTED, "nested too deeply"),
    ("leads.research", "findings", items(257), "too many items"),
    ("leads.research", "pages", "'{}'::jsonb", "must be a JSON array"),
    ("leads.research", "pages", f"jsonb_build_array({long_text(16384)})", "too large"),
    ("leads.research", "pages", NESTED, "nested too deeply"),
    ("leads.research", "pages", items(65), "too many items"),
    ("leads.research", "raw_cites", "10001", RANGE),
    ("leads.research", "raw_cites", "-1", RANGE),
    ("leads.research", "valid_cites", "10001", RANGE),
    ("leads.research", "replay_key", "repeat('A', 64)", FORM),
    ("leads.research", "cost_usd", "-1", RANGE),
    ("leads.research", "latency_ms", "100000001", RANGE),
    # inbox.messages
    ("inbox.messages", "message_id", long_text(999), TOO_LONG),
    ("inbox.messages", "message_id", "''", EMPTY),
    ("inbox.messages", "mailpit_id", long_text(201), TOO_LONG),
    ("inbox.messages", "mailpit_id", "''", EMPTY),
    ("inbox.messages", "from_header", long_text(1001), TOO_LONG),
    ("inbox.messages", "reply_to_header", long_text(2001), TOO_LONG),
    ("inbox.messages", "to_addr", long_text(2001), TOO_LONG),
    ("inbox.messages", "subject", long_text(2001), TOO_LONG),
    ("inbox.messages", "received_at", "'1980-01-01T00:00:00Z'", "too early"),
    ("inbox.messages", "received_at", "'2150-01-01T00:00:00Z'", "too late"),
    ("inbox.messages", "body_text", long_text(1_048_577), TOO_LONG),
    # inbox.triage
    ("inbox.triage", "category", "'gossip'", BAD_ENUM),
    ("inbox.triage", "priority", "'whenever'", BAD_ENUM),
    ("inbox.triage", "injection_reasons", "'{}'::jsonb", "must be a JSON array"),
    ("inbox.triage", "injection_reasons", f"jsonb_build_array({long_text(16384)})", "too large"),
    ("inbox.triage", "injection_reasons", NESTED, "nested too deeply"),
    ("inbox.triage", "injection_reasons", items(17), "too many items"),
    ("inbox.triage", "replay_key", "repeat('A', 64)", FORM),
    ("inbox.triage", "cost_usd", "-1", RANGE),
    ("inbox.triage", "latency_ms", "100000001", RANGE),
    # inbox.drafts
    ("inbox.drafts", "to_addr", long_text(321), TOO_LONG),
    ("inbox.drafts", "subject", long_text(999), TOO_LONG),
    ("inbox.drafts", "in_reply_to", long_text(999), TOO_LONG),
    ("inbox.drafts", "body", long_text(65537), TOO_LONG),
    ("inbox.drafts", "facts_used", "'{}'::jsonb", "must be a JSON array"),
    ("inbox.drafts", "facts_used", f"jsonb_build_array({long_text(16384)})", "too large"),
    ("inbox.drafts", "facts_used", NESTED, "nested too deeply"),
    ("inbox.drafts", "facts_used", items(65), "too many items"),
    ("inbox.drafts", "grounding", "'[]'::jsonb", "must be a JSON object"),
    ("inbox.drafts", "grounding", big_object(16384), "too large"),
    ("inbox.drafts", "grounding", NESTED_OBJECT, "nested too deeply"),
    ("inbox.drafts", "failure_reason", long_text(201), TOO_LONG),
    ("inbox.drafts", "replay_key", "repeat('A', 64)", FORM),
    ("inbox.drafts", "cost_usd", "-1", RANGE),
    ("inbox.drafts", "latency_ms", "100000001", RANGE),
    ("inbox.drafts", "sent_at", "now() + interval '2 days'", "too far in the future"),
    ("inbox.drafts", "sent_at", "now() - interval '2 days'", "too far in the past"),
]


@pytest.mark.parametrize(
    ("table", "column", "value", "problem"),
    REFUSALS,
    ids=[f"{t}.{c}-{p}-{i}" for i, (t, c, _, p) in enumerate(REFUSALS)],
)
def test_the_database_refuses_a_value_outside_its_bound(
    table: str, column: str, value: str, problem: str
) -> None:
    assert_refused(table, column, problem, **{column: value})


# --- a row stored before the bounds keeps working ---------------------------------------------


def test_a_grandfathered_row_keeps_working_for_unrelated_updates() -> None:
    run_id = str(uuid4())
    long_site = "repeat('w', 300)"
    try:
        seeded = psql(
            f"""
            alter table leads.research disable trigger research_bounds;
            insert into core.runs (id, workflow, mode) values ('{run_id}', 'leads', 'replay');
            insert into leads.research
                (run_id, company_name, city_hint, website, status, fields)
                values ('{run_id}', 'Grandfather Co', '', {long_site}, 'researched', '{{}}');
            alter table leads.research enable always trigger research_bounds;
            """
        )
        assert seeded.returncode == 0, seeded.stderr
        where = f"where run_id = '{run_id}'"

        unrelated = psql(
            f"update leads.research set reason = 'rechecked' {where}", role="opskit_app"
        )
        assert unrelated.returncode == 0, unrelated.stderr
        assert "UPDATE 1" in unrelated.stdout

        same_value = psql(
            f"update leads.research set website = {long_site} {where}", role="opskit_app"
        )
        assert same_value.returncode == 0, same_value.stderr

        longer = psql(
            f"update leads.research set website = repeat('w', 301) {where}", role="opskit_app"
        )
        assert longer.returncode != 0
        assert "research.website: too long" in longer.stderr

        fixed = psql(
            f"update leads.research set website = 'https://acme-plumbing.example' {where}",
            role="opskit_app",
        )
        assert fixed.returncode == 0, fixed.stderr
    finally:
        psql(
            f"alter table leads.research enable always trigger research_bounds; "
            f"delete from leads.research where run_id = '{run_id}'; "
            f"delete from core.runs where id = '{run_id}'"
        )
