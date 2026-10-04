"""Builders for the hostile-row matrix: one valid row per table, and a way to try a variant.

A case is `(case_id, overrides, refused_text)`: `overrides` maps a column to a SQL expression
that replaces the valid one, and `refused_text` is a fragment of the database's error, or None
when the row must get past every trigger and constraint. The attempt runs as the requester role
inside a block that always raises at the end, so nothing is ever kept.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.integration.conftest import psql

Case = tuple[str, dict[str, str], str | None]

INSERTED_OK = "inserted ok"
REPORTED = "reported: "


def q(text: str) -> str:
    """A SQL string literal (standard_conforming_strings is on: backslashes are literal)."""
    return "'" + text.replace("'", "''") + "'"


def jsonb(text: str) -> str:
    return q(text) + "::jsonb"


def after(seconds: int) -> str:
    """A time `seconds` from the database clock, with no time-zone arithmetic in it."""
    return f"now() + {seconds} * interval '1 second'"


def nested_object(levels: int) -> str:
    """An object whose deepest value sits `levels` levels below the top (`{"k":1}` is 1)."""
    arrays = levels - 1
    return '{"k":' + "[" * arrays + "1" + "]" * arrays + "}"


def deep_array_in_object(arrays: int) -> str:
    return '{"k":' + "[" * arrays + "]" * arrays + "}"


APPROVAL_BASE = {
    "action": q("kit_smoke.echo"),
    "summary": q("s"),
    "payload": "'{}'::jsonb",
    "payload_sha256": "repeat(md5(random()::text), 2)",
    "requested_by": q("service.n8n"),
    "required_role": q("approver"),
    "created_at": "now()",
    "expires_at": after(3600),
}
AUDIT_BASE = {
    # Huge and random, so a row that got in could never collide with a real record.
    "seq": "(9000000000000000000 - (random() * 1000000000000)::bigint)",
    "schema_version": "4",
    "event_id": "gen_random_uuid()",
    "occurred_at": "now()",
    "action": q("test.hostile"),
    "actor_id": q("service.n8n"),
    "subject_id": q("subject-1"),
    "payload": q("{}"),
    "run_context": "null",
    "prev_hash": "repeat('0', 64)",
    "record_hash": "encode(sha256(gen_random_uuid()::text::bytea), 'hex')",
}
RUNS_BASE = {
    "workflow": q("kit_smoke"),
    "n8n_workflow_id": "null",
    "n8n_execution_id": "'hostile-' || gen_random_uuid()::text",
    "mode": q("replay"),
}
MODEL_CALLS_BASE = {
    "prompt_id": q("triage_v1"),
    "prompt_version": "1",
    "tier": q("small"),
    "replay_key": "repeat('a', 64)",
    "model": "null",
    "mode": q("replay"),
    "input_tokens": "1",
    "output_tokens": "1",
    "cost_usd": "0.01",
    "latency_ms": "5",
}
SAMPLE_FILES_BASE = {
    "path": "'hostile/' || gen_random_uuid()::text || '.txt'",
    "workflow": q("receipts"),
    "kind": q("receipt_image"),
    "sha256": "repeat('a', 64)",
    "bytes": "10",
}


def insert_sql(table: str, base: dict[str, str], overrides: dict[str, str]) -> str:
    # An override may name a nullable column the base row leaves out; an unknown column is an
    # error from the database, which neither an accepted nor a refused case can mistake for success.
    row = {**base, **overrides}
    return f"insert into {table} ({', '.join(row)}) values ({', '.join(row.values())})"


def attempt(
    table: str,
    base: dict[str, str],
    overrides: dict[str, str],
    *,
    zone: str | None = None,
    report: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Insert as the requester role, then raise so nothing is kept. `report` is a SQL
    expression over the stored row `r` whose value is put in the final error text."""
    statement = insert_sql(table, base, overrides)
    if report is None:
        body = f"begin {statement}; raise exception '{INSERTED_OK}'; end"
    else:
        body = (
            f"declare r {table}; begin {statement} returning * into r; "
            f"raise exception '{REPORTED}%', {report}; end"
        )
    prefix = f"set time zone '{zone}'; " if zone is not None else ""
    return psql(f"{prefix}do $$ {body} $$", role="opskit_app")


def expect(result: subprocess.CompletedProcess[str], refused: str | None) -> None:
    """Refused with the named problem, or accepted (the block's own raise is the only error)."""
    assert result.returncode != 0, "the block always raises, so a success exit is impossible"
    if refused is None:
        assert INSERTED_OK in result.stderr, result.stderr
    else:
        assert INSERTED_OK not in result.stderr, "the row was accepted"
        assert refused in result.stderr, result.stderr


def params(cases: list[Case]) -> list[object]:
    return [pytest.param(overrides, refused, id=case_id) for case_id, overrides, refused in cases]
