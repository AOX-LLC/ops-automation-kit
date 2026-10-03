"""Controls the database itself enforces: append-only audit and role isolation."""

from __future__ import annotations

import pytest

from tests.integration.conftest import psql

pytestmark = pytest.mark.integration

# Each probe rolls back: if a control were ever missing, the test fails on the exit code and
# nothing is lost.
MUTATIONS = [
    "begin; update core.audit_log set action = 'tampered' "
    "where seq = (select min(seq) from core.audit_log); rollback;",
    "begin; delete from core.audit_log "
    "where seq = (select min(seq) from core.audit_log); rollback;",
    "begin; truncate core.audit_log; rollback;",
]


@pytest.mark.parametrize("sql", MUTATIONS)
def test_app_role_cannot_rewrite_the_audit_log(sql: str) -> None:
    result = psql(sql, role="opskit_app")
    assert result.returncode != 0
    assert "permission denied" in result.stderr


@pytest.mark.parametrize("role", ["opskit_owner", "postgres"])
@pytest.mark.parametrize("sql", MUTATIONS)
def test_even_owner_and_superuser_hit_the_append_only_trigger(role: str, sql: str) -> None:
    before = psql("select count(*), max(seq) from core.audit_log").stdout
    result = psql(sql, role=role)
    assert result.returncode != 0
    assert "append-only" in result.stderr
    assert psql("select count(*), max(seq) from core.audit_log").stdout == before


def test_app_role_can_append() -> None:
    sql = (
        "begin; insert into core.audit_log (seq, schema_version, event_id, occurred_at, action, "
        "actor_id, payload, prev_hash, record_hash) values (9000000000, 3, gen_random_uuid(), "
        "now(), 'itest.append', 'itest', '{}', repeat('0', 64), repeat('f', 64)); rollback;"
    )
    assert psql(sql, role="opskit_app").returncode == 0


@pytest.mark.parametrize(
    ("role", "db"),
    [("opskit_app", "n8n"), ("opskit_owner", "n8n"), ("n8n_user", "opskit")],
)
def test_roles_cannot_cross_into_the_other_database(role: str, db: str) -> None:
    result = psql("select 1", role=role, db=db)
    assert result.returncode != 0
    assert "permission denied for database" in result.stderr


@pytest.mark.parametrize(
    "sql",
    [
        "begin; create table core.itest_intruder (id int); rollback;",
        "begin; create table public.itest_intruder (id int); rollback;",
        "begin; drop table core.approvals; rollback;",
        "begin; alter table core.audit_log disable trigger audit_log_no_update_delete; rollback;",
    ],
)
def test_app_role_has_no_ddl(sql: str) -> None:
    result = psql(sql, role="opskit_app")
    assert result.returncode != 0
    assert "permission denied" in result.stderr or "must be owner" in result.stderr


def test_app_role_cannot_grant_itself_update_on_the_audit_log() -> None:
    # A non-owner GRANT "succeeds" with a warning and changes nothing; check the effect.
    psql("grant update, delete on core.audit_log to opskit_app", role="opskit_app")
    check = "select has_table_privilege('opskit_app', 'core.audit_log', 'UPDATE,DELETE')"
    assert psql(check).stdout.strip() == "f"


def test_app_role_is_not_privileged() -> None:
    row = psql(
        "select rolsuper, rolcreaterole, rolcreatedb, rolbypassrls from pg_roles "
        "where rolname in ('opskit_app', 'opskit_approver', 'opskit_owner', 'n8n_user') "
        "order by rolname"
    ).stdout.split()
    assert row == ["f|f|f|f"] * 4


def test_audit_hash_chain_verifies() -> None:
    """Every record links to its parent and matches its own hash (agent-core schema 2)."""
    from tests.integration.conftest import compose

    script = (
        "import asyncio\n"
        "from opskit.config import Settings\n"
        "from opskit.core.pg.audit import PgAuditLog\n"
        "from opskit.db.engine import make_engine, make_session_factory\n"
        "async def main():\n"
        "    engine = make_engine(Settings())\n"
        "    head = await PgAuditLog(make_session_factory(engine)).verify()\n"
        "    print(head.seq)\n"
        "    await engine.dispose()\n"
        "asyncio.run(main())\n"
    )
    result = compose("exec", "-T", "api", "python", "-c", script, check=False)
    assert result.returncode == 0, result.stderr[-500:]
    assert int(result.stdout.strip()) >= 1


@pytest.mark.parametrize(
    "sql",
    [
        "update inbox.drafts set body = 'changed' where false",
        "update inbox.drafts set to_addr = 'x@y.example' where false",
        "update inbox.triage set category = 'other' where false",
        "delete from inbox.drafts where false",
    ],
)
def test_app_role_cannot_rewrite_a_stored_draft_or_triage(sql: str) -> None:
    result = psql(sql, role="opskit_app")
    assert result.returncode != 0
    assert "permission denied" in result.stderr


def test_app_role_can_move_a_draft_and_hold_a_message() -> None:
    for sql in (
        "update inbox.drafts set status = status, approval_id = approval_id, sent_at = sent_at"
        " where false",
        "update inbox.triage set quarantined = quarantined, route = route where false",
    ):
        assert psql(sql, role="opskit_app").returncode == 0, sql


def test_db_role_is_set_by_the_database_and_cannot_be_forged() -> None:
    """A version 3 record carries the role that inserted it, whatever the insert said. The block
    raises at the end so nothing is kept in the hash chain."""
    sql = (
        "do $$ declare stored text; begin "
        "insert into core.audit_log (seq, schema_version, event_id, occurred_at, action, "
        "actor_id, payload, prev_hash, record_hash, db_role) values (9000000001, 3, "
        "gen_random_uuid(), now(), 'itest.append', 'itest', '{}', repeat('0', 64), "
        "repeat('e', 64), 'opskit_approver') returning db_role into stored; "
        "raise exception 'stored db_role=%', stored; end $$"
    )
    result = psql(sql, role="opskit_app")
    assert "stored db_role=opskit_app" in result.stderr, result.stderr


def test_a_new_record_cannot_claim_schema_2_to_dodge_attribution() -> None:
    v2 = (
        "begin; insert into core.audit_log (seq, schema_version, event_id, occurred_at, action, "
        "actor_id, payload, prev_hash, record_hash) values (9000000002, 2, gen_random_uuid(), "
        "now(), 'itest.append', 'itest', '{}', repeat('0', 64), repeat('d', 64)); rollback;"
    )
    for role in ("opskit_app", "opskit_approver", "opskit_owner", "postgres"):
        result = psql(v2, role=role)
        assert result.returncode != 0, role
        assert "must be schema 3" in result.stderr or "permission denied" in result.stderr


def test_every_version_3_record_names_its_role_and_older_ones_have_none() -> None:
    wrong = psql(
        "select count(*) from core.audit_log where (schema_version = 3) <> (db_role is not null)"
    )
    assert wrong.stdout.strip() == "0"
