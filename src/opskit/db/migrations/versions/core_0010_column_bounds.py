"""core: database bounds on every column the requester role can write, in core.

`core.enforce_bounds()` is one trigger function driven by a JSON spec (see opskit.db.bounds). It
refuses an insert, or an update that changes the column, when a value breaks its rule: text
length and form, enum membership, number and time ranges, JSON type, size, depth and item count.
`core.bounds_violations()` runs the same rules over stored rows, for the report below and for
tests. Phase 3d's domain tables (crm, receipts, leads, inbox) attach the same function in their own
revisions.

Rows already stored are never rewritten and never fail the upgrade:

- A bound is checked on insert and on a changed column only, so a stored row that breaks one keeps
  working for unrelated updates.
- This revision reports, per table, the keys and the kind of problem of stored rows outside the new
  bounds (never the values: they may be client data).
- Approvals that are still live (pending, or approved and not yet used) and outside the bounds are
  cancelled, as core_0006 cancelled approved ones: nobody should be asked to decide a request the
  database would now refuse, and a reader cannot be relied on to parse it. The table is locked
  first and the guard is off for that one statement, inside this transaction. Finished approvals
  stay as they are; readers cope with them (opskit.core.pg.approvals).
- core.audit_log is append-only and untouched: the new insert bounds apply to new records.

Revision ID: core_0010
Revises: core_0009
"""

from alembic import op

from opskit.db import bounds as b

revision = "core_0010"
down_revision = "core_0009"
branch_labels = None
depends_on = None

ID = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$"
PRINCIPAL = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
SUBJECT = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$"
ACTION = r"^[a-z][a-z0-9_]*([.][a-z][a-z0-9_]*)*$"
EXTERNAL_ID_NAME = r"^[a-z][a-z0-9_]{0,63}$"
HEX64 = r"^[0-9a-f]{64}$"
N8N_ID = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$"
DAY = 86400

RUNS = {
    "n8n_workflow_id": b.text(64, regex=N8N_ID, nullable=True),
    "n8n_execution_id": b.text(64, regex=N8N_ID, nullable=True),
    "finished_at": b.moment(max_s=300, nullable=True),
}
APPROVALS = {
    "payload": b.document("object", max_bytes=32768, depth=8),
    "resume_url": b.text(512, regex=r"^https?://[^[:space:]]+$", nullable=True),
    "run_context": b.document("object", max_bytes=2048, depth=3, nullable=True),
    "run_context.external_ids": b.id_map(
        16, key_regex=EXTERNAL_ID_NAME, value_regex=ID, nullable=True
    ),
    "delegates": b.document("array", max_bytes=4096, depth=2, items=16),
}
AUDIT_LOG = {
    "action": b.text(100, min=1, regex=ACTION),
    "actor_id": b.text(128, min=1, regex=PRINCIPAL),
    "subject_id": b.text(200, min=1, regex=SUBJECT, nullable=True),
    "payload": b.json_text("object", max_bytes=8192, depth=6),
    "run_context": b.json_text("object", max_bytes=2048, depth=3, nullable=True),
}
MODEL_CALLS = {
    "prompt_id": b.text(100, min=1, regex=r"^(adhoc|[a-z][a-z0-9_.-]{0,99})$"),
    "prompt_version": b.number(0, 10000),
    "tier": b.enum("small", "mid", "large"),
    "replay_key": b.text(64, min=64, regex=HEX64),
    "model": b.text(100, nullable=True),
    "mode": b.enum("replay", "live", "record"),
    "input_tokens": b.number(0, 1_000_000_000),
    "output_tokens": b.number(0, 1_000_000_000),
    "cost_usd": b.number(0, 10000),
    "latency_ms": b.number(0, 100_000_000),
}
SAMPLE_FILES = {
    "path": b.text(512, min=1, regex=r"^(?!/)(?!.*[.][.]).+$"),
    "workflow": b.enum("receipts", "leads", "inbox"),
    "kind": b.enum(
        "receipt_image",
        "bank_csv",
        "company_list",
        "corpus_doc",
        "crm_accounts",
        "email",
        "business_profile",
    ),
    "sha256": b.text(64, min=64, regex=HEX64),
    "bytes": b.number(0, 2_147_483_647),
}

# (table, trigger, spec, key columns); approvals are insert-time bounds, the guard fixes the rest.
CORE_BOUNDS = [
    ("core.runs", "runs_bounds", RUNS, ["id"]),
    ("core.approvals", "approvals_bounds", APPROVALS, ["id"]),
    ("core.model_calls", "model_calls_bounds", MODEL_CALLS, ["id"]),
    ("core.sample_files", "sample_files_bounds", SAMPLE_FILES, ["path"]),
]

FUNCTIONS = r"""
-- NULL if the value is within the rule, else a short description of the problem (no values).
CREATE FUNCTION core.bound_problem(val jsonb, rule jsonb) RETURNS text
    LANGUAGE plpgsql STABLE
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    kind text := rule ->> 'kind';
    s text;
    n numeric;
    t timestamptz;
    parsed jsonb;
    relative double precision;
BEGIN
    IF val IS NULL OR jsonb_typeof(val) = 'null' THEN
        IF coalesce((rule ->> 'nullable')::boolean, false) THEN
            RETURN NULL;
        END IF;
        RETURN 'must not be null';
    END IF;

    IF kind = 'text' THEN
        IF jsonb_typeof(val) <> 'string' THEN RETURN 'must be text'; END IF;
        s := val #>> '{}';
        IF length(s) < coalesce((rule ->> 'min')::int, 0) THEN RETURN 'too short'; END IF;
        IF length(s) > (rule ->> 'max')::int THEN RETURN 'too long'; END IF;
        IF rule ? 'regex' AND s !~ (rule ->> 'regex') THEN
            RETURN 'not in the expected form';
        END IF;

    ELSIF kind = 'enum' THEN
        IF jsonb_typeof(val) <> 'string' OR NOT (rule -> 'values' @> val) THEN
            RETURN 'not an allowed value';
        END IF;

    ELSIF kind = 'number' THEN
        IF jsonb_typeof(val) <> 'number' THEN RETURN 'must be a number'; END IF;
        n := (val #>> '{}')::numeric;
        IF n < (rule ->> 'min')::numeric OR n > (rule ->> 'max')::numeric THEN
            RETURN 'out of range';
        END IF;

    ELSIF kind = 'moment' THEN
        IF jsonb_typeof(val) <> 'string' THEN RETURN 'must be a time'; END IF;
        BEGIN
            t := (val #>> '{}')::timestamptz;
        EXCEPTION WHEN others THEN
            RETURN 'not a time';
        END;
        IF t IN ('infinity', '-infinity') THEN RETURN 'not a finite time'; END IF;
        IF rule ? 'after' AND t < (rule ->> 'after')::timestamptz THEN RETURN 'too early'; END IF;
        IF rule ? 'before' AND t > (rule ->> 'before')::timestamptz THEN RETURN 'too late'; END IF;
        relative := extract(epoch FROM t) - extract(epoch FROM statement_timestamp());
        IF rule ? 'min_s' AND relative < (rule ->> 'min_s')::double precision THEN
            RETURN 'too far in the past';
        END IF;
        IF rule ? 'max_s' AND relative > (rule ->> 'max_s')::double precision THEN
            RETURN 'too far in the future';
        END IF;

    ELSIF kind IN ('document', 'json_text') THEN
        IF kind = 'json_text' THEN
            IF jsonb_typeof(val) <> 'string' THEN RETURN 'must be text'; END IF;
            s := val #>> '{}';
            IF octet_length(s) > (rule ->> 'max_bytes')::int THEN RETURN 'too large'; END IF;
            BEGIN
                parsed := s::jsonb;
            EXCEPTION WHEN others THEN
                RETURN 'not JSON, or nested too deeply';
            END;
        ELSE
            parsed := val;
            IF octet_length(parsed::text) > (rule ->> 'max_bytes')::int THEN
                RETURN 'too large';
            END IF;
        END IF;
        IF jsonb_typeof(parsed) <> (rule ->> 'type') THEN
            RETURN 'must be a JSON ' || (rule ->> 'type');
        END IF;
        -- ".**{N to last}" visits every value N or more levels down, without recursing.
        IF jsonb_path_exists(
               parsed, ('$.**{' || ((rule ->> 'depth')::int + 1) || ' to last}')::jsonpath) THEN
            RETURN 'nested too deeply';
        END IF;
        IF rule ? 'items' AND jsonb_typeof(parsed) = 'array'
           AND jsonb_array_length(parsed) > (rule ->> 'items')::int THEN
            RETURN 'too many items';
        END IF;

    ELSIF kind = 'id_map' THEN
        IF jsonb_typeof(val) <> 'object' THEN RETURN 'must be a JSON object'; END IF;
        IF (SELECT count(*) FROM jsonb_object_keys(val)) > (rule ->> 'max_items')::int THEN
            RETURN 'too many entries';
        END IF;
        IF EXISTS (SELECT 1 FROM jsonb_each(val) AS e(key, value)
                   WHERE e.key !~ (rule ->> 'key_regex')
                      OR jsonb_typeof(e.value) <> 'string'
                      OR (e.value #>> '{}') !~ (rule ->> 'value_regex')) THEN
            RETURN 'an entry is not in the expected form';
        END IF;

    ELSE
        RAISE EXCEPTION 'unknown bound kind %', kind;
    END IF;
    RETURN NULL;
END;
$$;

-- The value of a column, or of a member of a JSON column ("column.member"), as jsonb.
CREATE FUNCTION core.bound_value(row_json jsonb, path text) RETURNS jsonb
    LANGUAGE sql IMMUTABLE
    SET search_path = pg_catalog, pg_temp
AS $$
    SELECT row_json #> string_to_array(path, '.')
$$;

CREATE FUNCTION core.enforce_bounds() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    spec jsonb := TG_ARGV[0]::jsonb;
    new_row jsonb := to_jsonb(NEW);
    old_row jsonb;
    path text;
    rule jsonb;
    problem text;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        old_row := to_jsonb(OLD);
    END IF;
    FOR path, rule IN SELECT e.key, e.value FROM jsonb_each(spec) AS e(key, value) LOOP
        -- An update is checked only where it changed the value, so a row stored before the
        -- bound existed is not refused for an unrelated change.
        IF TG_OP = 'UPDATE'
           AND core.bound_value(new_row, path) IS NOT DISTINCT FROM core.bound_value(old_row, path)
        THEN
            CONTINUE;
        END IF;
        problem := core.bound_problem(core.bound_value(new_row, path), rule);
        IF problem IS NOT NULL THEN
            RAISE EXCEPTION '%.%: %', TG_TABLE_NAME, path, problem
                USING ERRCODE = 'check_violation';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$;

-- One text per problem: "<key values> | <column> | <problem>". Reads every stored row.
CREATE FUNCTION core.bounds_violations(tbl regclass, spec jsonb, key_columns text[])
    RETURNS SETOF text
    LANGUAGE plpgsql STABLE
    SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    row_json jsonb;
    path text;
    rule jsonb;
    problem text;
    key_text text;
BEGIN
    FOR row_json IN EXECUTE format('SELECT to_jsonb(t) FROM %s t', tbl) LOOP
        key_text := (SELECT string_agg(row_json ->> k, ',') FROM unnest(key_columns) AS k);
        FOR path, rule IN SELECT e.key, e.value FROM jsonb_each(spec) AS e(key, value) LOOP
            problem := core.bound_problem(core.bound_value(row_json, path), rule);
            IF problem IS NOT NULL THEN
                RETURN NEXT key_text || ' | ' || path || ' | ' || problem;
            END IF;
        END LOOP;
    END LOOP;
END;
$$;
"""

CANCEL_LIVE_OUTSIDE_BOUNDS = """
    ALTER TABLE core.approvals DISABLE TRIGGER approvals_guard;
    DO $cancel$
    DECLARE
        cancelled uuid[];
    BEGIN
        WITH outside AS (
            SELECT DISTINCT split_part(v, ' | ', 1)::uuid AS id
            FROM core.bounds_violations('core.approvals'::regclass, {spec}, ARRAY['id']) v),
        closed AS (
            UPDATE core.approvals a
            SET status = 'cancelled', closed_at = now(), decision = NULL,
                resolved_by = NULL, resolved_at = NULL,
                reason = 'closed by the 3d bounds: a stored value is outside the new limits'
            FROM outside o
            WHERE a.id = o.id AND a.status IN ('pending', 'approved')
            RETURNING a.id)
        SELECT coalesce(array_agg(id), '{{}}') INTO cancelled FROM closed;
        RAISE WARNING 'bounds: % live approval(s) outside the new limits cancelled: %',
            cardinality(cancelled), cancelled;
    END
    $cancel$;
    ALTER TABLE core.approvals ENABLE ALWAYS TRIGGER approvals_guard;
"""

AUDIT_BOUNDS_TRIGGER = "audit_log_bounds"


def upgrade() -> None:
    # Nothing may change an approval while the upgrade decides which ones to close.
    op.execute("LOCK TABLE core.approvals IN EXCLUSIVE MODE")
    op.execute(FUNCTIONS)
    for table, _, spec, key in CORE_BOUNDS:
        op.execute(b.report_sql(table, spec, key))
    op.execute(CANCEL_LIVE_OUTSIDE_BOUNDS.format(spec=b.literal(APPROVALS)))
    for table, name, spec, _ in CORE_BOUNDS:
        op.execute(b.attach_sql(table, name, spec))
    # The audit log takes inserts only, and only through its own writer.
    op.execute(b.attach_sql("core.audit_log", AUDIT_BOUNDS_TRIGGER, AUDIT_LOG, events="INSERT"))


def downgrade() -> None:
    op.execute(b.detach_sql("core.audit_log", AUDIT_BOUNDS_TRIGGER))
    for table, name, _, _ in reversed(CORE_BOUNDS):
        op.execute(b.detach_sql(table, name))
    op.execute(
        """
        DROP FUNCTION core.bounds_violations(regclass, jsonb, text[]);
        DROP FUNCTION core.enforce_bounds();
        DROP FUNCTION core.bound_value(jsonb, text);
        DROP FUNCTION core.bound_problem(jsonb, jsonb);
        """
    )
