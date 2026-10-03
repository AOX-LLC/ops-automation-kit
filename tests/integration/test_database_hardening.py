"""Phase 3d: the database refuses what the application would never write.

Each test goes to the database directly, as the role under test, so it holds even if the
application code is wrong or bypassed.
"""

from __future__ import annotations

import secrets

import pytest

from tests.integration.conftest import psql

pytestmark = pytest.mark.integration

SESSION_COLUMNS = "(csrf_token, expires_at)"


def new_session(*, role: str = "opskit_approver", hours: int = 1) -> str:
    token = secrets.token_urlsafe(32)
    result = psql(
        f"insert into core.approver_sessions {SESSION_COLUMNS} "
        f"values ('{token}', now() + interval '{hours} hours') returning id",
        role=role,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[0]


def stored(session_id: str, columns: str) -> str:
    result = psql(f"select {columns} from core.approver_sessions where id = '{session_id}'")
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


# --- F3: approver sessions -----------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "select csrf_token from core.approver_sessions",
        "select id from core.approver_sessions",
        "update core.approver_sessions set revoked_at = null",
        "update core.approver_sessions set revoked_at = now()",
        f"insert into core.approver_sessions {SESSION_COLUMNS} "
        "values (repeat('a', 32), now() + interval '1 hour')",
    ],
)
def test_the_requester_role_has_no_access_to_sessions(sql: str) -> None:
    result = psql(sql, role="opskit_app")
    assert result.returncode != 0
    assert "permission denied" in result.stderr


def test_a_revoked_session_cannot_be_brought_back() -> None:
    session_id = new_session()
    revoke = psql(
        f"update core.approver_sessions set revoked_at = now() where id = '{session_id}'",
        role="opskit_approver",
    )
    assert revoke.returncode == 0, revoke.stderr
    for sql in (
        f"update core.approver_sessions set revoked_at = null where id = '{session_id}'",
        "update core.approver_sessions set revoked_at = now() + interval '1 day' "
        f"where id = '{session_id}'",
    ):
        undone = psql(sql, role="opskit_approver")
        assert undone.returncode != 0
        assert "stays revoked" in undone.stderr
    assert stored(session_id, "revoked_at is not null") == "t"


def test_revocation_is_stamped_by_the_database_whatever_the_caller_sends() -> None:
    session_id = new_session()
    psql(
        f"update core.approver_sessions set revoked_at = '2000-01-01' where id = '{session_id}'",
        role="opskit_approver",
    )
    assert stored(session_id, "revoked_at > now() - interval '1 minute'") == "t"


@pytest.mark.parametrize(
    "assignment",
    [
        "expires_at = expires_at + interval '1 day'",
        "csrf_token = repeat('b', 32)",
        "created_at = now() - interval '1 year'",
        "id = gen_random_uuid()",
    ],
)
def test_a_session_changes_only_by_being_revoked(assignment: str) -> None:
    session_id = new_session()
    result = psql(
        f"update core.approver_sessions set {assignment} where id = '{session_id}'",
        role="opskit_approver",
    )
    assert result.returncode != 0
    assert "permission denied" in result.stderr or "changes only by being revoked" in result.stderr


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ("('short', now() + interval '1 hour')", "shape the application refuses"),
        ("(repeat('a', 129), now() + interval '1 hour')", "shape the application refuses"),
        ("('has spaces in it, so no good', now() + interval '1 hour')", "shape"),
        ("(repeat('a', 32), now() - interval '1 second')", "in the future and within 30 days"),
        ("(repeat('a', 32), now() + interval '31 days')", "in the future and within 30 days"),
        ("(repeat('a', 32), 'infinity')", "in the future and within 30 days"),
    ],
)
def test_a_session_needs_a_well_formed_token_and_a_sane_expiry(values: str, message: str) -> None:
    result = psql(
        f"insert into core.approver_sessions {SESSION_COLUMNS} values {values}",
        role="opskit_approver",
    )
    assert result.returncode != 0
    assert message in result.stderr


def test_a_new_session_is_created_by_the_database_clock() -> None:
    result = psql(
        "insert into core.approver_sessions (csrf_token, expires_at, created_at) "
        "values (repeat('c', 32), now() + interval '1 hour', '1999-01-01') returning id",
        role="opskit_approver",
    )
    assert result.returncode == 0, result.stderr
    session_id = result.stdout.strip().splitlines()[0]
    assert stored(session_id, "created_at > now() - interval '1 minute'") == "t"


def test_sessions_are_never_deleted_or_truncated() -> None:
    session_id = new_session()
    deleted = psql(
        f"delete from core.approver_sessions where id = '{session_id}'", role="opskit_approver"
    )
    assert deleted.returncode != 0
    owner_delete = psql(f"delete from core.approver_sessions where id = '{session_id}'")
    assert "never deleted" in owner_delete.stderr
    assert "never truncated" in psql("truncate core.approver_sessions").stderr
