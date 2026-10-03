"""Hostile values for every column the requester role can write, tried straight at the database.

For each column a value is either refused by the database (the bound) or accepted, and an
accepted one is stated on purpose: readers must cope with it. Every attempt runs as `opskit_app`
inside a block that raises at the end, so nothing is kept. The pattern is agent-core's
`test_untrusted_columns` matrix, rewritten for this schema and the 3d bounds.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from opskit.db.bounds import literal
from opskit.db.migrations.versions.core_0010_column_bounds import APPROVALS
from tests.integration.conftest import psql
from tests.integration.hostile import (
    APPROVAL_BASE,
    AUDIT_BASE,
    MODEL_CALLS_BASE,
    REPORTED,
    RUNS_BASE,
    SAMPLE_FILES_BASE,
    Case,
    after,
    attempt,
    deep_array_in_object,
    expect,
    jsonb,
    nested_object,
    params,
    q,
)

pytestmark = pytest.mark.integration

WEEK = 7 * 24 * 3600
LONG_ACTION = "a" + "b" * 99
LONG_NAME = "r" + "x" * 63
LONG_PRINCIPAL = "p" + "q" * 127
SHAPE = "shape the application refuses"
FORM = "not in the expected form"
LIFETIME = "lives at most 7 days"
# Bidirectional overrides, an ANSI colour escape and a bidi isolate: 500 characters in all.
_BIDI = "Pay ‮elbaT‬ \x1b[31mred\x1b[0m ⁦x⁩ "
BIDI_SUMMARY = _BIDI + "y" * (500 - len(_BIDI))


def ids(count: int, *, value: str = "v") -> str:
    entries = ",".join(f'"n{i}":"{value}{i}"' for i in range(count))
    return '{"run_id":"r1","external_ids":{' + entries + "}}"


def external(name: str, value: str) -> str:
    return jsonb('{"run_id":"r1","external_ids":{"' + name + '":' + value + "}}")


# --- core.approvals: what a requester can insert --------------------------------------------

APPROVAL_CASES: list[Case] = [
    ("action-at-100", {"action": q(LONG_ACTION)}, None),
    ("action-at-101", {"action": q(LONG_ACTION + "c")}, SHAPE),
    ("action-uppercase", {"action": q("Kit.Echo")}, SHAPE),
    ("summary-500-with-bidi-and-ansi", {"summary": q(BIDI_SUMMARY)}, None),
    ("summary-501", {"summary": q("s" * 501)}, SHAPE),
    ("summary-empty", {"summary": q("")}, SHAPE),
    ("requested-by-128", {"requested_by": q(LONG_PRINCIPAL)}, None),
    ("requested-by-129", {"requested_by": q(LONG_PRINCIPAL + "q")}, SHAPE),
    ("requested-by-with-at-sign", {"requested_by": q("a@b")}, SHAPE),
    ("required-role-64", {"required_role": q(LONG_NAME)}, None),
    ("required-role-65", {"required_role": q(LONG_NAME + "x")}, SHAPE),
    ("sha-uppercase", {"payload_sha256": q("A" * 64)}, SHAPE),
    ("sha-non-hex", {"payload_sha256": q("g" * 64)}, SHAPE),
    ("sha-63-chars", {"payload_sha256": q("a" * 63)}, SHAPE),
    # Lifetime in elapsed seconds: the guard owns both ends of the interval.
    ("lifetime-exactly-168-hours", {"expires_at": after(WEEK)}, None),
    ("lifetime-168-hours-and-a-second", {"expires_at": after(WEEK + 1)}, LIFETIME),
    ("lifetime-zero", {"expires_at": "now()"}, LIFETIME),
    ("lifetime-negative", {"expires_at": after(-3600)}, LIFETIME),
    ("expires-infinity", {"expires_at": q("infinity")}, LIFETIME),
    ("expires-minus-infinity", {"expires_at": q("-infinity")}, LIFETIME),
    # Accepted, but the stored created_at is the database's (see test_created_at_is_replaced).
    (
        "created-in-the-far-past",
        {"created_at": q("2000-01-01 00:00:00+00"), "expires_at": q("2000-01-01 01:00:00+00")},
        None,
    ),
    (
        "created-in-the-far-future",
        {"created_at": q("2100-01-01 00:00:00+00"), "expires_at": q("2100-01-01 01:00:00+00")},
        None,
    ),
    (
        "created-at-24-hundred",
        {"created_at": q("2026-10-03T24:00:00Z"), "expires_at": q("2026-10-04T01:00:00Z")},
        None,
    ),
    (
        "created-at-a-leap-second",
        {"created_at": q("2026-06-30T23:59:60Z"), "expires_at": q("2026-07-01T01:00:00Z")},
        None,
    ),
    (
        "delegates-16-duplicates",
        {"delegates": jsonb("[" + ",".join(['"agent-intake"'] * 16) + "]")},
        None,
    ),
    (
        "delegates-16-at-128-chars",
        {"delegates": jsonb("[" + ",".join([f'"{LONG_PRINCIPAL}"'] * 16) + "]")},
        None,
    ),
    (
        "delegates-17",
        {"delegates": jsonb("[" + ",".join(['"d"'] * 17) + "]")},
        "delegates: too many items",
    ),
    ("delegates-non-string-element", {"delegates": jsonb("[1]")}, SHAPE),
    ("delegates-element-129-chars", {"delegates": jsonb(f'["{LONG_PRINCIPAL}q"]')}, SHAPE),
    (
        "delegates-5000-byte-array",
        {"delegates": jsonb("[" + ",".join(['"' + "d" * 300 + '"'] * 16) + "]")},
        "delegates: too large",
    ),
    ("run-context-sql-null", {"run_context": "null"}, None),
    ("run-context-array", {"run_context": jsonb("[1]")}, "run_context: must be a JSON object"),
    ("run-context-string", {"run_context": jsonb('"x"')}, "run_context: must be a JSON object"),
    ("run-context-json-null-literal", {"run_context": jsonb("null")}, SHAPE),
    ("run-context-16-external-ids", {"run_context": jsonb(ids(16))}, None),
    (
        "run-context-17-external-ids",
        {"run_context": jsonb(ids(17))},
        "run_context.external_ids: too many entries",
    ),
    (
        "run-context-uppercase-id-name",
        {"run_context": external("Abc", '"x"')},
        "run_context.external_ids: an entry is not in the expected form",
    ),
    (
        "run-context-id-name-leading-digit",
        {"run_context": external("1abc", '"x"')},
        "run_context.external_ids: an entry is not in the expected form",
    ),
    (
        "run-context-id-name-65-chars",
        {"run_context": external("a" + "b" * 64, '"x"')},
        "run_context.external_ids: an entry is not in the expected form",
    ),
    (
        "run-context-id-name-64-chars",
        {"run_context": external("a" + "b" * 63, '"x"')},
        None,
    ),
    (
        "run-context-numeric-id-value",
        {"run_context": external("abc", "1")},
        "run_context.external_ids: an entry is not in the expected form",
    ),
    (
        "run-context-id-value-with-a-space",
        {"run_context": external("abc", '"a b"')},
        "run_context.external_ids: an entry is not in the expected form",
    ),
    # The name matches the pattern, so the database accepts it. Readers must cope: nothing may
    # treat an external id's name as safe to display or log because the table accepted it.
    (
        "run-context-secret-shaped-name",
        {"run_context": jsonb('{"run_id":"r1","external_ids":{"api_key":"abc"}}')},
        None,
    ),
    (
        "run-context-over-2048-bytes",
        {"run_context": jsonb(ids(16, value="x" * 150))},
        "run_context: too large",
    ),
    ("payload-json-array", {"payload": jsonb("[1]")}, "payload: must be a JSON object"),
    ("payload-json-string", {"payload": jsonb('"x"')}, "payload: must be a JSON object"),
    ("payload-json-number", {"payload": jsonb("1")}, "payload: must be a JSON object"),
    ("payload-json-null-literal", {"payload": jsonb("null")}, "payload: must not be null"),
    # jsonb prints {"k": "xxx"}: nine characters of frame around the value.
    (
        "payload-exactly-at-131072-bytes",
        {"payload": "jsonb_build_object('k', repeat('x', 131063))"},
        None,
    ),
    (
        "payload-131073-bytes",
        {"payload": "jsonb_build_object('k', repeat('x', 131064))"},
        "payload: too large",
    ),
    ("payload-8-levels", {"payload": jsonb(nested_object(8))}, None),
    ("payload-9-levels", {"payload": jsonb(nested_object(9))}, "payload: nested too deeply"),
    (
        "payload-1500-levels-in-an-object",
        {"payload": jsonb(deep_array_in_object(1500))},
        "payload: nested too deeply",
    ),
    # jsonb keeps the last of two equal keys (see test_duplicate_keys_keep_the_last).
    ("payload-duplicate-keys", {"payload": jsonb('{"a":1,"a":2}')}, None),
    # Postgres refuses a lone surrogate when it parses the literal, before any trigger.
    ("payload-lone-surrogate-escape", {"payload": jsonb('{"a":"\\ud800"}')}, "surrogate"),
    ("payload-1e400", {"payload": jsonb('{"n":1e400}')}, None),
    ("resume-url-null", {"resume_url": "null"}, None),
    ("resume-url-512", {"resume_url": q("http://x.example/" + "a" * 495)}, None),
    (
        "resume-url-513",
        {"resume_url": q("http://x.example/" + "a" * 496)},
        "resume_url: too long",
    ),
    ("resume-url-with-a-space", {"resume_url": q("http://x.example/a b")}, FORM),
    ("resume-url-with-a-newline", {"resume_url": q("http://x.example/a\nb")}, FORM),
    ("resume-url-ftp", {"resume_url": q("ftp://x.example/a")}, FORM),
]

# (id, session time zone, overrides, refused): the cap counts elapsed seconds in any zone.
ZONED_LIFETIME_CASES: list[tuple[str, str, dict[str, str], str | None]] = [
    ("plus-14-168-hours", "+14", {"expires_at": after(WEEK)}, None),
    ("plus-14-168-hours-and-a-second", "+14", {"expires_at": after(WEEK + 1)}, LIFETIME),
    ("minus-12-168-hours", "-12", {"expires_at": after(WEEK)}, None),
    ("minus-12-168-hours-and-a-second", "-12", {"expires_at": after(WEEK + 1)}, LIFETIME),
    ("new-york-168-hours", "America/New_York", {"expires_at": after(WEEK)}, None),
    (
        "new-york-168-hours-and-a-second",
        "America/New_York",
        {"expires_at": after(WEEK + 1)},
        LIFETIME,
    ),
    # Clocks go back on 2 November 2025: the local week from the 29th is 169 hours long, and
    # only elapsed time counts, so it is refused; the local week that is 168 hours is accepted.
    (
        "new-york-fall-back-week-is-169-hours",
        "America/New_York",
        {
            "created_at": "timestamptz '2025-10-29 12:00:00'",
            "expires_at": "timestamptz '2025-11-05 12:00:00'",
        },
        LIFETIME,
    ),
    (
        "new-york-fall-back-168-elapsed-hours",
        "America/New_York",
        {
            "created_at": "timestamptz '2025-10-29 12:00:00'",
            "expires_at": "timestamptz '2025-11-05 11:00:00'",
        },
        None,
    ),
]


@pytest.mark.parametrize(("overrides", "refused"), params(APPROVAL_CASES))
def test_approval_insert(overrides: dict[str, str], refused: str | None) -> None:
    expect(attempt("core.approvals", APPROVAL_BASE, overrides), refused)


@pytest.mark.parametrize(
    ("zone", "overrides", "refused"),
    [pytest.param(z, o, r, id=i) for i, z, o, r in ZONED_LIFETIME_CASES],
)
def test_approval_lifetime_in_a_session_time_zone(
    zone: str, overrides: dict[str, str], refused: str | None
) -> None:
    expect(attempt("core.approvals", APPROVAL_BASE, overrides, zone=zone), refused)


STAMPED_CASES = [
    (
        "far-past",
        {"created_at": q("2000-01-01 00:00:00+00"), "expires_at": q("2000-01-01 01:00:00+00")},
    ),
    (
        "far-future",
        {"created_at": q("2100-01-01 00:00:00+00"), "expires_at": q("2100-01-01 01:00:00+00")},
    ),
    (
        "24-hundred",
        {"created_at": q("2026-10-03T24:00:00Z"), "expires_at": q("2026-10-04T01:00:00Z")},
    ),
    (
        "leap-second",
        {"created_at": q("2026-06-30T23:59:60Z"), "expires_at": q("2026-07-01T01:00:00Z")},
    ),
]


@pytest.mark.parametrize("overrides", [pytest.param(o, id=i) for i, o in STAMPED_CASES])
def test_created_at_is_replaced_by_the_databases_and_the_lifetime_is_kept(
    overrides: dict[str, str],
) -> None:
    result = attempt(
        "core.approvals",
        APPROVAL_BASE,
        overrides,
        report=(
            "(abs(extract(epoch from r.created_at) - extract(epoch from statement_timestamp())) "
            "< 5)::text || ' ' || "
            "round(extract(epoch from r.expires_at) - extract(epoch from r.created_at))::text"
        ),
    )
    assert f"{REPORTED}true 3600" in result.stderr, result.stderr


def test_duplicate_keys_keep_the_last() -> None:
    result = attempt(
        "core.approvals",
        APPROVAL_BASE,
        {"payload": jsonb('{"a":1,"a":2}')},
        report="r.payload ->> 'a'",
    )
    assert f"{REPORTED}2" in result.stderr, result.stderr


@pytest.mark.parametrize("arrays", [1500, 6000], ids=["1500-levels", "6000-levels"])
def test_a_deeply_nested_payload_is_refused_and_the_server_stays_up(arrays: int) -> None:
    result = attempt(
        "core.approvals", APPROVAL_BASE, {"payload": jsonb(deep_array_in_object(arrays))}
    )
    expect_refused = result.returncode != 0 and "inserted ok" not in result.stderr
    assert expect_refused, result.stderr
    assert "ERROR" in result.stderr
    alive = psql("select 1")
    assert alive.returncode == 0 and alive.stdout.strip() == "1", alive.stderr


# --- core.audit_log: what a requester can append ---------------------------------------------


def text_object(filler: int) -> str:
    """SQL for a JSON object as text: 8 bytes of frame around `filler` bytes of value."""
    return "'{\"k\":\"' || repeat('x', " + str(filler) + ") || '\"}'"


AUDIT_CASES: list[Case] = [
    ("action-at-100", {"action": q(LONG_ACTION)}, None),
    ("action-at-101", {"action": q(LONG_ACTION + "c")}, "action: too long"),
    ("action-uppercase", {"action": q("Bad.Action")}, f"action: {FORM}"),
    ("action-empty", {"action": q("")}, "action: too short"),
    ("actor-at-128", {"actor_id": q(LONG_PRINCIPAL)}, None),
    ("actor-at-129", {"actor_id": q(LONG_PRINCIPAL + "q")}, "actor_id: too long"),
    ("actor-with-at-sign", {"actor_id": q("a@b")}, f"actor_id: {FORM}"),
    ("subject-at-200", {"subject_id": q("s" * 200)}, None),
    ("subject-at-201", {"subject_id": q("s" * 201)}, "subject_id: too long"),
    ("subject-with-at-sign", {"subject_id": q("a@b")}, f"subject_id: {FORM}"),
    ("subject-null", {"subject_id": "null"}, None),
    (
        "payload-exactly-8192-bytes",
        {"payload": text_object(8184)},
        None,
    ),
    (
        "payload-8193-bytes",
        {"payload": text_object(8185)},
        "payload: too large",
    ),
    ("payload-json-array", {"payload": q("[1]")}, "payload: must be a JSON object"),
    ("payload-not-json", {"payload": q("not json")}, "payload: not JSON"),
    ("payload-depth-6", {"payload": q(nested_object(6))}, None),
    ("payload-depth-7", {"payload": q(nested_object(7))}, "payload: nested too deeply"),
    # 4000 levels fit in 8192 bytes, so the depth rule (or the parser's own limit) refuses.
    (
        "payload-4000-levels-in-a-string",
        {"payload": "'{\"a\":' || repeat('[', 4000) || repeat(']', 4000) || '}'"},
        "too deeply",
    ),
    # 5000 levels are 10006 bytes: the size rule refuses before anything parses it.
    (
        "payload-5000-levels-in-a-string",
        {"payload": "'{\"a\":' || repeat('[', 5000) || repeat(']', 5000) || '}'"},
        "payload: too large",
    ),
    ("run-context-sql-null", {"run_context": "null"}, None),
    ("run-context-object", {"run_context": q('{"run_id":"r1"}')}, None),
    (
        "run-context-over-2048-bytes",
        {"run_context": text_object(2100)},
        "run_context: too large",
    ),
    ("run-context-array", {"run_context": q("[1]")}, "run_context: must be a JSON object"),
    (
        "run-context-json-null-text",
        {"run_context": q("null")},
        "run_context: must be a JSON object",
    ),
]


@pytest.mark.parametrize(("overrides", "refused"), params(AUDIT_CASES))
def test_audit_insert(overrides: dict[str, str], refused: str | None) -> None:
    expect(attempt("core.audit_log", AUDIT_BASE, overrides), refused)


# --- core.runs --------------------------------------------------------------------------------

N8N_ID_64 = "a" + "b" * 63

RUNS_CASES: list[Case] = [
    ("workflow-id-64", {"n8n_workflow_id": q(N8N_ID_64)}, None),
    ("workflow-id-65", {"n8n_workflow_id": q(N8N_ID_64 + "b")}, "n8n_workflow_id: too long"),
    ("workflow-id-with-a-space", {"n8n_workflow_id": q("a b")}, f"n8n_workflow_id: {FORM}"),
    ("workflow-id-with-at-sign", {"n8n_workflow_id": q("a@b")}, f"n8n_workflow_id: {FORM}"),
    ("execution-id-64", {"n8n_execution_id": q(N8N_ID_64)}, None),
    ("execution-id-65", {"n8n_execution_id": q(N8N_ID_64 + "b")}, "n8n_execution_id: too long"),
    ("execution-id-with-a-space", {"n8n_execution_id": q("a b")}, f"n8n_execution_id: {FORM}"),
    ("execution-id-with-at-sign", {"n8n_execution_id": q("a@b")}, f"n8n_execution_id: {FORM}"),
    ("execution-id-null", {"n8n_execution_id": "null"}, None),
    ("finished-an-hour-ahead", {"finished_at": after(3600)}, "finished_at: too far in the future"),
    ("finished-four-minutes-ahead", {"finished_at": after(240)}, None),
    ("finished-yesterday", {"finished_at": after(-86400)}, None),
    ("finished-infinity", {"finished_at": q("infinity")}, "finished_at: not a finite time"),
    ("finished-minus-infinity", {"finished_at": q("-infinity")}, "finished_at: not a finite time"),
    ("finished-before-2000", {"finished_at": q("1999-12-31T23:59:59Z")}, "finished_at: too early"),
    ("n8n-workflow-id-empty", {"n8n_workflow_id": q("")}, None),
    ("n8n-execution-id-empty", {"n8n_execution_id": q("")}, None),
    ("workflow-not-listed", {"workflow": q("evil")}, "violates check constraint"),
    ("mode-not-listed", {"mode": q("mock")}, "violates check constraint"),
]


@pytest.mark.parametrize(("overrides", "refused"), params(RUNS_CASES))
def test_runs_insert(overrides: dict[str, str], refused: str | None) -> None:
    expect(attempt("core.runs", RUNS_BASE, overrides), refused)


# --- core.model_calls -------------------------------------------------------------------------

MODEL_CALLS_CASES: list[Case] = [
    ("prompt-id-adhoc", {"prompt_id": q("adhoc")}, None),
    ("prompt-id-normal", {"prompt_id": q("inbox.triage_v2-b")}, None),
    ("prompt-id-100", {"prompt_id": q("a" * 100)}, None),
    ("prompt-id-101", {"prompt_id": q("a" * 101)}, "prompt_id: too long"),
    ("prompt-id-uppercase", {"prompt_id": q("Triage")}, f"prompt_id: {FORM}"),
    ("prompt-id-empty", {"prompt_id": q("")}, "prompt_id: too short"),
    ("prompt-version-0", {"prompt_version": "0"}, None),
    ("prompt-version-10000", {"prompt_version": "10000"}, None),
    ("prompt-version-minus-1", {"prompt_version": "-1"}, "prompt_version: out of range"),
    ("prompt-version-10001", {"prompt_version": "10001"}, "prompt_version: out of range"),
    ("tier-not-listed", {"tier": q("huge")}, "tier: not an allowed value"),
    ("mode-not-listed", {"mode": q("mock")}, "mode: not an allowed value"),
    ("replay-key-63-hex", {"replay_key": q("a" * 63)}, f"replay_key: {FORM}"),
    ("replay-key-65-hex", {"replay_key": q("a" * 65)}, "too long"),
    ("replay-key-uppercase", {"replay_key": q("A" * 64)}, f"replay_key: {FORM}"),
    ("model-100", {"model": q("m" * 100)}, None),
    ("model-101", {"model": q("m" * 101)}, "model: too long"),
    ("input-tokens-minus-1", {"input_tokens": "-1"}, "input_tokens: out of range"),
    ("input-tokens-a-billion", {"input_tokens": "1000000000"}, None),
    ("input-tokens-over-a-billion", {"input_tokens": "1000000001"}, "input_tokens: out of range"),
    ("output-tokens-minus-1", {"output_tokens": "-1"}, "output_tokens: out of range"),
    ("cost-negative", {"cost_usd": "-0.000001"}, "cost_usd: out of range"),
    ("cost-at-the-column-maximum", {"cost_usd": "9999.999999"}, None),
    # numeric(10,6) tops out below the bound of 10000, so the column refuses before the trigger.
    ("cost-1e12", {"cost_usd": "1000000000000"}, "overflow"),
    ("latency-negative", {"latency_ms": "-1"}, "latency_ms: out of range"),
    ("latency-over-bound", {"latency_ms": "100000001"}, "latency_ms: out of range"),
]


@pytest.mark.parametrize(("overrides", "refused"), params(MODEL_CALLS_CASES))
def test_model_calls_insert(overrides: dict[str, str], refused: str | None) -> None:
    expect(attempt("core.model_calls", MODEL_CALLS_BASE, overrides), refused)


# --- core.sample_files ------------------------------------------------------------------------

SAMPLE_FILES_CASES: list[Case] = [
    ("path-512", {"path": q("a" * 512)}, None),
    ("path-513", {"path": q("a" * 513)}, "path: too long"),
    ("path-empty", {"path": q("")}, "path: too short"),
    ("path-parent-segment", {"path": q("../etc/passwd")}, f"path: {FORM}"),
    ("path-parent-in-the-middle", {"path": q("a/../b")}, f"path: {FORM}"),
    ("path-leading-slash", {"path": q("/etc/passwd")}, f"path: {FORM}"),
    ("path-nested-ok", {"path": q("receipts/2026/a.png")}, None),
    ("path-double-dot-in-a-file-name", {"path": q("receipts/inbox/a..png")}, None),
    ("path-parent-segment-at-the-end", {"path": q("a/..")}, f"path: {FORM}"),
    ("workflow-not-listed", {"workflow": q("kit_smoke")}, "workflow: not an allowed value"),
    ("workflow-leads", {"workflow": q("leads"), "kind": q("company_list")}, None),
    ("workflow-inbox", {"workflow": q("inbox"), "kind": q("email")}, None),
    ("kind-not-listed", {"kind": q("virus")}, "kind: not an allowed value"),
    ("sha256-63", {"sha256": q("a" * 63)}, f"sha256: {FORM}"),
    ("sha256-65", {"sha256": q("a" * 65)}, "too long"),
    ("sha256-uppercase", {"sha256": q("A" * 64)}, f"sha256: {FORM}"),
    ("bytes-negative", {"bytes": "-1"}, "bytes: out of range"),
    ("bytes-zero", {"bytes": "0"}, None),
    ("bytes-int-maximum", {"bytes": "2147483647"}, None),
]


@pytest.mark.parametrize(("overrides", "refused"), params(SAMPLE_FILES_CASES))
def test_sample_files_insert(overrides: dict[str, str], refused: str | None) -> None:
    expect(attempt("core.sample_files", SAMPLE_FILES_BASE, overrides), refused)


# --- invariants the database keeps whatever the reader does ------------------------------------


@pytest.fixture
def uuid_marker() -> str:
    return f"hostile-{uuid4()}"


def count_with_summary(marker: str) -> str:
    result = psql(f"select count(*) from core.approvals where summary = '{marker}'")
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def approval_row(marker: str, **overrides: str) -> str:
    row = {**APPROVAL_BASE, "summary": q(marker), **overrides}
    return f"insert into core.approvals ({', '.join(row)}) values ({', '.join(row.values())})"


def test_a_refused_insert_leaves_no_row(uuid_marker: str) -> None:
    refused = psql(approval_row(uuid_marker, payload=jsonb("[1]")), role="opskit_app")
    assert refused.returncode != 0
    assert "payload: must be a JSON object" in refused.stderr
    assert count_with_summary(uuid_marker) == "0"


def test_a_refused_row_takes_the_valid_row_before_it_down_with_it(uuid_marker: str) -> None:
    """Two statements in one -c run as one transaction: the good row is not kept either."""
    sql = (
        f"{approval_row(uuid_marker)}; {approval_row(uuid_marker, payload=jsonb(nested_object(9)))}"
    )
    refused = psql(sql, role="opskit_app")
    assert refused.returncode != 0
    assert "nested too deeply" in refused.stderr
    assert count_with_summary(uuid_marker) == "0"


def test_a_refused_run_leaves_no_row(uuid_marker: str) -> None:
    sql = (
        "insert into core.runs (workflow, n8n_execution_id, mode) "
        f"values ('kit_smoke', 'hostile-{uuid_marker}', 'replay'); "
        "insert into core.runs (workflow, n8n_execution_id, mode) "
        f"values ('kit_smoke', 'hostile {uuid_marker}', 'replay')"
    )
    refused = psql(sql, role="opskit_app")
    assert refused.returncode != 0
    assert FORM in refused.stderr
    kept = psql(
        f"select count(*) from core.runs where n8n_execution_id like 'hostile%{uuid_marker}'"
    )
    assert kept.stdout.strip() == "0"


def planted_row(approval_id: str, **overrides: str) -> str:
    """A cancelled approval, inserted past both triggers, so it is never a live request."""
    row = {
        **APPROVAL_BASE,
        "summary": q("planted for the bounds report"),
        "status": q("cancelled"),
        "closed_at": "now()",
        **overrides,
    }
    row = {"id": q(approval_id), **row}
    return f"insert into core.approvals ({', '.join(row)}) values ({', '.join(row.values())})"


def test_bounds_violations_returns_exactly_the_planted_rows() -> None:
    """Planted the way the grandfather test does: a superuser switches the two triggers off for
    this one transaction and back on (always-on) before it commits, so a failure rolls the whole
    call back and leaves them on. The rows go in already cancelled, so none is ever pending."""
    bad_payload, bad_context, clean = (str(uuid4()) for _ in range(3))
    sql = "; ".join(
        [
            "alter table core.approvals disable trigger approvals_bounds",
            "alter table core.approvals disable trigger approvals_guard",
            planted_row(bad_payload, payload=jsonb("[1]")),
            planted_row(
                bad_context,
                delegates=jsonb("[" + ",".join(['"' + "d" * 300 + '"'] * 16) + "]"),
                run_context=jsonb('{"run_id":"r1","external_ids":{"Bad":"x"}}'),
            ),
            planted_row(clean),
            "alter table core.approvals enable always trigger approvals_bounds",
            "alter table core.approvals enable always trigger approvals_guard",
        ]
    )
    planted = psql(sql)
    assert planted.returncode == 0, planted.stderr

    report = psql(
        "select * from core.bounds_violations("
        f"'core.approvals', {literal(dict(APPROVALS))}, array['id'])"
    )
    assert report.returncode == 0, report.stderr
    mine = {bad_payload, bad_context, clean}
    found = {line for line in report.stdout.splitlines() if line.split(" | ")[0] in mine}
    assert found == {
        f"{bad_payload} | payload | must be a JSON object",
        f"{bad_context} | delegates | too large",
        f"{bad_context} | run_context.external_ids | an entry is not in the expected form",
    }

    live = psql(
        "select count(*) from core.approvals "
        f"where id in ('{bad_payload}', '{bad_context}', '{clean}') and status <> 'cancelled'"
    )
    assert live.stdout.strip() == "0"
    triggers = psql(
        "select string_agg(tgname || '=' || tgenabled::text, ',' order by tgname) from pg_trigger "
        "where tgrelid = 'core.approvals'::regclass "
        "and tgname in ('approvals_bounds', 'approvals_guard')"
    )
    assert triggers.stdout.strip() == "approvals_bounds=A,approvals_guard=A"
