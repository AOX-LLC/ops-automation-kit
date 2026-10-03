"""Phase 3d: the database refuses what the application would never write.

Each test goes to the database directly, as the role under test, so it holds even if the
application code is wrong or bypassed.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest

from tests.integration.conftest import ApproverClient, psql

MakeApproval = Callable[..., dict[str, Any]]

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


# --- F6: the outbox ------------------------------------------------------------------------

DECIDE = (
    "update core.approvals set status = '{status}', decision = '{decision}', "
    "resolved_by = 'a.person', resolved_at = now() where id = '{id}'; "
)
QUEUE = (
    "insert into core.outbox (approval_id, payload{extra_columns}) "
    "values ('{id}', '{payload}'{extra_values})"
)


def payload_for(approval_id: str, decision: str = "approve", **more: Any) -> str:
    return json.dumps({"approval_id": approval_id, "decision": decision, **more})


def queue_sql(approval_id: str, payload: str, **columns: str) -> str:
    return QUEUE.format(
        id=approval_id,
        payload=payload,
        extra_columns="".join(f", {name}" for name in columns),
        extra_values="".join(f", {value}" for value in columns.values()),
    )


def as_approver(sql: str) -> Any:
    return psql(sql, role="opskit_approver")


def test_a_resume_is_refused_for_an_approval_that_is_still_pending(
    make_approval: MakeApproval,
) -> None:
    approval_id = make_approval()["approval_id"]
    result = as_approver(queue_sql(approval_id, payload_for(approval_id)))
    assert result.returncode != 0
    assert "only an approved or rejected approval has a resume to queue" in result.stderr


def test_a_resume_is_refused_for_an_approval_that_does_not_exist() -> None:
    missing = str(uuid4())
    result = as_approver(queue_sql(missing, payload_for(missing)))
    assert result.returncode != 0
    assert "no such approval to resume" in result.stderr


@pytest.mark.parametrize(
    ("status", "decision", "claimed", "extra"),
    [
        ("approved", "approve", "reject", {}),
        ("rejected", "reject", "approve", {}),
        ("approved", "approve", "approve", {"note": "x"}),
        ("approved", "approve", "Approve", {}),
    ],
    ids=["approved-claims-reject", "rejected-claims-approve", "extra-key", "wrong-case"],
)
def test_a_resume_must_carry_exactly_the_stored_decision(
    make_approval: MakeApproval, status: str, decision: str, claimed: str, extra: dict[str, str]
) -> None:
    approval_id = make_approval()["approval_id"]
    sql = DECIDE.format(status=status, decision=decision, id=approval_id) + queue_sql(
        approval_id, payload_for(approval_id, claimed, **extra)
    )
    result = as_approver(sql)
    assert result.returncode != 0
    assert "must name this approval and its stored decision" in result.stderr
    # One statement block is one transaction: the refusal rolled the decision back too.
    assert psql(f"select status from core.approvals where id = '{approval_id}'").stdout.strip() == (
        "pending"
    )


def test_a_resume_cannot_name_a_different_approval_in_its_payload(
    make_approval: MakeApproval,
) -> None:
    approval_id = make_approval()["approval_id"]
    sql = DECIDE.format(status="approved", decision="approve", id=approval_id) + queue_sql(
        approval_id, payload_for(str(uuid4()))
    )
    assert "must name this approval" in as_approver(sql).stderr


@pytest.mark.parametrize(
    "columns",
    [
        {"delivered_at": "now()"},
        {"attempts": "1"},
        {"last_error": "'boom'"},
    ],
    ids=["delivered", "tried", "errored"],
)
def test_a_new_resume_is_undelivered_and_untried(
    make_approval: MakeApproval, columns: dict[str, str]
) -> None:
    approval_id = make_approval()["approval_id"]
    sql = DECIDE.format(status="approved", decision="approve", id=approval_id) + queue_sql(
        approval_id, payload_for(approval_id), **columns
    )
    result = as_approver(sql)
    assert result.returncode != 0
    assert "undelivered, untried" in result.stderr


def test_a_matching_decision_and_resume_in_one_transaction_is_accepted(
    make_approval: MakeApproval,
) -> None:
    approval_id = make_approval()["approval_id"]
    sql = DECIDE.format(status="rejected", decision="reject", id=approval_id) + queue_sql(
        approval_id, payload_for(approval_id, "reject")
    )
    result = as_approver(sql)
    assert result.returncode == 0, result.stderr
    count = psql(f"select count(*) from core.outbox where approval_id = '{approval_id}'")
    assert count.stdout.strip() == "1"


def test_the_requester_role_cannot_queue_a_resume(make_approval: MakeApproval) -> None:
    approval_id = make_approval()["approval_id"]
    result = psql(queue_sql(approval_id, payload_for(approval_id)), role="opskit_app")
    assert result.returncode != 0
    assert "permission denied" in result.stderr


def test_the_decision_path_still_queues_exactly_one_resume(
    approver: ApproverClient, make_approval: MakeApproval
) -> None:
    approval_id = make_approval()["approval_id"]
    assert approver.decide(approval_id, "approve", csrf=approver.page_csrf()).status_code == 303
    row = psql(f"select payload::text from core.outbox where approval_id = '{approval_id}'")
    assert json.loads(row.stdout.strip()) == {"approval_id": approval_id, "decision": "approve"}


@pytest.fixture
def queued_resume(approver: ApproverClient, make_approval: MakeApproval) -> str:
    approval_id = make_approval()["approval_id"]
    assert approver.decide(approval_id, "approve", csrf=approver.page_csrf()).status_code == 303
    return str(approval_id)


def as_requester(sql: str) -> Any:
    return psql(sql, role="opskit_app")


def bump(approval_id: str, assignment: str) -> str:
    return f"update core.outbox set {assignment} where approval_id = '{approval_id}'; "


@pytest.mark.parametrize(
    ("assignment", "message"),
    [
        ("attempts = 11", "attempts only go up, to at most 10"),
        ("last_error = repeat('e', 201)", "at most 200 characters"),
        ("next_attempt_at = now() + interval '2 hours'", "at most an hour away"),
    ],
)
def test_the_resume_worker_cannot_write_outside_the_delivery_bounds(
    queued_resume: str, assignment: str, message: str
) -> None:
    result = as_requester(bump(queued_resume, assignment))
    assert result.returncode != 0
    assert message in result.stderr


def test_attempts_never_go_back_down(queued_resume: str) -> None:
    # One block is one transaction, so the api's own worker cannot interleave.
    result = as_requester(bump(queued_resume, "attempts = 5") + bump(queued_resume, "attempts = 4"))
    assert result.returncode != 0
    assert "attempts only go up" in result.stderr


def test_delivery_is_stamped_by_the_database_and_cannot_be_cleared(queued_resume: str) -> None:
    stamped = as_requester(
        bump(queued_resume, "delivered_at = '2000-01-01'")
        + f"select delivered_at > now() - interval '1 minute' from core.outbox "
        f"where approval_id = '{queued_resume}'"
    )
    assert stamped.returncode == 0, stamped.stderr
    assert stamped.stdout.strip().splitlines()[-1] == "t"
    cleared = as_requester(
        bump(queued_resume, "delivered_at = now()") + bump(queued_resume, "delivered_at = null")
    )
    assert cleared.returncode != 0
    assert "stays delivered" in cleared.stderr


# --- F7: the guard stamps every time -------------------------------------------------------

LONG_AGO = "'2000-01-01'"


def near_now(column: str, approval_id: str) -> str:
    return psql(
        f"select abs(extract(epoch from {column}) - extract(epoch from now())) < 120 "
        f"from core.approvals where id = '{approval_id}'"
    ).stdout.strip()


def test_resolved_at_is_the_databases_whatever_the_approver_sends(
    make_approval: MakeApproval,
) -> None:
    approval_id = make_approval()["approval_id"]
    result = as_approver(
        f"update core.approvals set status = 'approved', decision = 'approve', "
        f"resolved_by = 'a.person', resolved_at = {LONG_AGO} where id = '{approval_id}'"
    )
    assert result.returncode == 0, result.stderr
    assert near_now("resolved_at", approval_id) == "t"


def test_a_decision_with_no_resolved_at_is_stamped_too(make_approval: MakeApproval) -> None:
    approval_id = make_approval()["approval_id"]
    result = as_approver(
        "update core.approvals set status = 'rejected', decision = 'reject', "
        f"resolved_by = 'a.person', resolved_at = null where id = '{approval_id}'"
    )
    assert result.returncode == 0, result.stderr
    assert near_now("resolved_at", approval_id) == "t"


def test_closed_at_on_a_cancellation_is_the_databases(make_approval: MakeApproval) -> None:
    approval_id = make_approval()["approval_id"]
    result = as_requester(
        f"update core.approvals set status = 'cancelled', closed_at = {LONG_AGO} "
        f"where id = '{approval_id}'"
    )
    assert result.returncode == 0, result.stderr
    assert near_now("closed_at", approval_id) == "t"


def test_consumed_at_is_the_databases(queued_resume: str) -> None:
    result = as_requester(
        f"update core.approvals set status = 'consumed', consumed_at = {LONG_AGO} "
        f"where id = '{queued_resume}'"
    )
    assert result.returncode == 0, result.stderr
    assert near_now("consumed_at", queued_resume) == "t"


def test_closed_at_on_an_expiry_is_the_rows_own_expires_at(make_approval: MakeApproval) -> None:
    approval_id = make_approval()["approval_id"]
    # Ageing needs the owner (the guard fixes expires_at); the expiry itself must come from the
    # requester role, so these are two sessions and the api's 60 s sweep may expire the row in
    # between. That is fine: it stores the same value, and the assertion is on the stored row.
    ageing = (
        "alter table core.approvals disable trigger approvals_guard; "
        "update core.approvals set expires_at = now() - interval '1 hour', "
        f"created_at = now() - interval '2 hours' where id = '{approval_id}'; "
        "alter table core.approvals enable always trigger approvals_guard"
    )
    assert psql(ageing).returncode == 0
    as_requester(
        f"update core.approvals set status = 'expired', closed_at = now() "
        f"where id = '{approval_id}'"
    )
    stored = psql(
        f"select status, closed_at = expires_at from core.approvals where id = '{approval_id}'"
    )
    assert stored.stdout.strip() == "expired|t"


def test_an_approvals_lifetime_is_still_capped_and_positive() -> None:
    for lifetime in (
        "interval '7 days' + interval '1 second'",
        "interval '0'",
        "interval '-1 hour'",
    ):
        sql = (
            "do $$ begin insert into core.approvals (action, summary, payload, payload_sha256, "
            "requested_by, required_role, created_at, expires_at) values ('kit_smoke.echo', 's', "
            f"'{{}}', repeat('0', 64), 'service.n8n', 'approver', now(), now() + {lifetime}); "
            "raise exception 'inserted ok'; end $$"
        )
        assert "at most 7 days" in as_requester(sql).stderr
    infinite = as_requester(
        "do $$ begin insert into core.approvals (action, summary, payload, payload_sha256, "
        "requested_by, required_role, created_at, expires_at) values ('kit_smoke.echo', 's', "
        "'{}', repeat('0', 64), 'service.n8n', 'approver', now(), 'infinity'); "
        "raise exception 'inserted ok'; end $$"
    )
    assert "at most 7 days" in infinite.stderr
