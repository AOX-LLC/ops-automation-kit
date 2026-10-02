"""Controls the database itself enforces: append-only audit and role isolation."""

from __future__ import annotations

import pytest

from tests.integration.conftest import psql

pytestmark = pytest.mark.integration

MUTATIONS = [
    "update core.audit_log set action = 'tampered' where id = (select min(id) from core.audit_log)",
    "delete from core.audit_log where id = (select min(id) from core.audit_log)",
    "truncate core.audit_log",
]


@pytest.mark.parametrize("sql", MUTATIONS)
def test_app_role_cannot_rewrite_the_audit_log(sql: str) -> None:
    result = psql(sql, role="opskit_app")
    assert result.returncode != 0
    assert "permission denied" in result.stderr


@pytest.mark.parametrize("role", ["opskit_owner", "postgres"])
@pytest.mark.parametrize("sql", MUTATIONS)
def test_even_owner_and_superuser_hit_the_append_only_trigger(role: str, sql: str) -> None:
    before = psql("select count(*), max(id) from core.audit_log").stdout
    result = psql(sql, role=role)
    assert result.returncode != 0
    assert "append-only" in result.stderr
    assert psql("select count(*), max(id) from core.audit_log").stdout == before


def test_app_role_can_append() -> None:
    sql = "insert into core.audit_log (actor, action) values ('itest', 'itest.append') returning id"
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
        "create table core.itest_intruder (id int)",
        "create table public.itest_intruder (id int)",
        "drop table core.approvals",
        "alter table core.audit_log disable trigger audit_log_no_update_delete",
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
        "where rolname in ('opskit_app', 'opskit_owner', 'n8n_user') order by rolname"
    ).stdout.split()
    assert row == ["f|f|f|f"] * 3
