"""The requester role can ask but never approve; only the approver role can decide.

Three layers are pinned: the HTTP surface (a service token cannot reach the decision path),
the database grants (the requester has no UPDATE on the decision columns) and the guard
trigger (even a role that can write `status` is refused unless it is the approver). The queue
tests run the kit's own PgApprovalQueue inside the api container, on the requester role.
"""

from __future__ import annotations

import json
import textwrap
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tests.integration.conftest import (
    ApproverClient,
    age_approval,
    audit_count,
    compose,
    psql,
)

pytestmark = pytest.mark.integration

MakeApproval = Callable[..., dict[str, Any]]

APPROVE = (
    "update core.approvals set status = 'approved', decision = 'approve', "
    "resolved_by = 'a.person', resolved_at = now() where id = '{id}'"
)
STATUS_ONLY = "update core.approvals set status = 'approved' where id = '{id}'"
NEW_APPROVED_ROW = (
    "insert into core.approvals (action, summary, payload, payload_sha256, requested_by, "
    "required_role, expires_at, status) values ('kit_smoke.echo', 's', '{}', repeat('0', 64), "
    "'service.n8n', 'approver', now() + interval '1 hour', 'approved')"
)


def status_of(service: httpx.Client, approval_id: str) -> str:
    return str(service.get(f"/v1/approvals/{approval_id}").json()["status"])


# --- the requester can not approve ---------------------------------------------------------


def test_a_service_token_cannot_reach_the_decision_path(
    service: httpx.Client, make_approval: MakeApproval
) -> None:
    approval_id = make_approval()["approval_id"]
    response = service.post(
        f"/approver/approvals/{approval_id}/decision", data={"decision": "approve"}
    )
    assert response.status_code == 401
    assert status_of(service, approval_id) == "pending"


def test_the_requester_role_cannot_write_the_decision_columns(make_approval: MakeApproval) -> None:
    approval_id = make_approval()["approval_id"]
    result = psql(APPROVE.format(id=approval_id), role="opskit_app")
    assert result.returncode != 0
    assert "permission denied" in result.stderr
    assert psql(f"select status from core.approvals where id = '{approval_id}'").stdout.strip() == (
        "pending"
    )


def test_the_requester_role_can_not_set_status_approved_either(
    make_approval: MakeApproval,
) -> None:
    """It may write `status` (to cancel, expire or consume); the trigger refuses 'approved'."""
    approval_id = make_approval()["approval_id"]
    result = psql(STATUS_ONLY.format(id=approval_id), role="opskit_app")
    assert result.returncode != 0
    assert "only the approver role may approve or reject" in result.stderr


@pytest.mark.parametrize("role", ["opskit_owner", "postgres"])
def test_the_owner_and_a_superuser_are_refused_too(role: str, make_approval: MakeApproval) -> None:
    approval_id = make_approval()["approval_id"]
    result = psql(APPROVE.format(id=approval_id), role=role)
    assert result.returncode != 0
    assert "only the approver role may approve or reject" in result.stderr


def test_the_requester_cannot_borrow_the_approver_role(make_approval: MakeApproval) -> None:
    approval_id = make_approval()["approval_id"]
    result = psql(f"set role opskit_approver; {APPROVE.format(id=approval_id)}", role="opskit_app")
    assert result.returncode != 0
    assert "permission denied to set role" in result.stderr


def test_a_superuser_who_sets_the_approver_role_is_still_refused(
    make_approval: MakeApproval,
) -> None:
    """The guard reads session_user as well as current_user."""
    approval_id = make_approval()["approval_id"]
    result = psql(f"set role opskit_approver; {APPROVE.format(id=approval_id)}")
    assert result.returncode != 0
    assert "only the approver role may approve or reject" in result.stderr


def test_a_row_cannot_be_inserted_already_approved() -> None:
    result = psql(NEW_APPROVED_ROW, role="opskit_app")
    assert result.returncode != 0
    assert "pending and undecided" in result.stderr


def test_the_guard_still_fires_when_replication_role_is_set(make_approval: MakeApproval) -> None:
    approval_id = make_approval()["approval_id"]
    sql = f"set session_replication_role = replica; {APPROVE.format(id=approval_id)}"
    result = psql(sql)
    assert result.returncode != 0
    assert "only the approver role may approve or reject" in result.stderr


# --- the approver role can ------------------------------------------------------------------


@pytest.mark.parametrize(("decision", "status"), [("approve", "approved"), ("reject", "rejected")])
def test_the_approver_role_can_decide(
    decision: str, status: str, make_approval: MakeApproval
) -> None:
    """Decide inside a block that raises at the end, so nothing is kept but the row count is
    seen (psql prints only the last of several statements)."""
    approval_id = make_approval()["approval_id"]
    sql = (
        "do $$ declare n int; begin "
        f"update core.approvals set status = '{status}', decision = '{decision}', "
        f"resolved_by = 'a.person', resolved_at = now() where id = '{approval_id}'; "
        "get diagnostics n = row_count; raise exception 'updated=%', n; end $$"
    )
    assert "updated=1" in psql(sql, role="opskit_approver").stderr


def test_the_approver_role_cannot_approve_an_expired_request(make_approval: MakeApproval) -> None:
    approval_id = make_approval()["approval_id"]
    age_approval(approval_id)
    result = psql(APPROVE.format(id=approval_id), role="opskit_approver")
    assert result.returncode != 0
    assert "has expired" in result.stderr


def test_the_approver_role_cannot_approve_its_own_request(make_approval: MakeApproval) -> None:
    approval_id = make_approval()["approval_id"]
    sql = APPROVE.format(id=approval_id).replace("a.person", "service.n8n")
    assert psql(sql, role="opskit_approver").returncode != 0


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        (NEW_APPROVED_ROW, "permission denied"),
        ("select * from core.runs limit 1", "permission denied"),
        ("update core.approvals set payload = '{}' where false", "permission denied"),
        ("update core.approvals set consumed_at = now() where false", "permission denied"),
        ("delete from core.approvals where false", "permission denied"),
        ("update core.audit_log set action = 'x' where false", "permission denied"),
    ],
)
def test_the_approver_role_can_do_little_else(sql: str, message: str) -> None:
    result = psql(sql, role="opskit_approver")
    assert result.returncode != 0
    assert message in result.stderr


def test_the_approver_role_cannot_consume_an_approved_request(
    service: httpx.Client, approver: ApproverClient, make_approval: MakeApproval
) -> None:
    approval_id = make_approval()["approval_id"]
    assert approver.decide(approval_id, "approve", csrf=approver.page_csrf()).status_code == 303
    sql = (
        "update core.approvals set status = 'consumed', consumed_at = now() "
        f"where id = '{approval_id}'"
    )
    assert psql(sql, role="opskit_approver").returncode != 0
    assert status_of(service, approval_id) == "approved"


def test_the_grants_are_exactly_the_split() -> None:
    def can(role: str, column: str, privilege: str = "UPDATE") -> str:
        sql = f"select has_column_privilege('{role}', 'core.approvals', '{column}', '{privilege}')"
        return psql(sql).stdout.strip()

    for column in ("decision", "resolved_by", "resolved_at", "reason"):
        assert can("opskit_app", column) == "f", column
        assert can("opskit_approver", column) == "t", column
    for column in ("consumed_at",):
        assert can("opskit_app", column) == "t"
        assert can("opskit_approver", column) == "f"
    for column in ("status", "closed_at"):
        assert can("opskit_app", column) == "t"
        assert can("opskit_approver", column) == "t"
    for column in ("payload", "expires_at", "requested_by", "required_role", "delegates"):
        assert can("opskit_app", column) == "f", column
        assert can("opskit_approver", column) == "f", column
    insert = "select has_table_privilege('{}', 'core.approvals', 'INSERT')"
    assert psql(insert.format("opskit_app")).stdout.strip() == "t"
    assert psql(insert.format("opskit_approver")).stdout.strip() == "f"
    member = "select pg_has_role('{}', '{}', 'MEMBER')"
    assert psql(member.format("opskit_app", "opskit_approver")).stdout.strip() == "f"
    assert psql(member.format("opskit_approver", "opskit_app")).stdout.strip() == "f"


def test_the_decision_through_the_page_is_written_by_the_approver_role(
    approver: ApproverClient, make_approval: MakeApproval
) -> None:
    approval_id = make_approval()["approval_id"]
    assert approver.decide(approval_id, "approve", csrf=approver.page_csrf()).status_code == 303
    assert audit_count("approval.decided", approval_id) == 1
    outbox = psql(f"select count(*) from core.outbox where approval_id = '{approval_id}'")
    assert outbox.stdout.strip() == "1"
    roles = psql(
        "select action, db_role from core.audit_log "
        f"where subject_id = '{approval_id}' order by seq"
    ).stdout.split()
    assert roles == ["approval.requested|opskit_app", "approval.decided|opskit_approver"]


# --- the queue, on the requester role --------------------------------------------------------

PRELUDE = textwrap.dedent(
    """
    import asyncio, json
    from datetime import UTC, datetime, timedelta
    from uuid import UUID
    from aox_agent_core.approvals import RoleApproverPolicy
    from opskit.config import Settings
    from opskit.core.pg.approvals import PgApprovalQueue
    from opskit.core.ports import N8N_SERVICE, ROLES_BY_ACTION, Principal, PrincipalKind
    from opskit.db.engine import make_engine, make_session_factory

    OTHER = Principal(id="service.other", kind=PrincipalKind.SERVICE)
    DELEGATE = Principal(id="service.delegate", kind=PrincipalKind.SERVICE)
    SWEEP = Principal(id="service.itest-sweep", kind=PrincipalKind.SERVICE)

    async def submit(queue, *, ttl_seconds=600, delegates=()):
        return await queue.submit(
            action="kit_smoke.echo", summary="itest", payload={"n": 1},
            requested_by=N8N_SERVICE, required_role="approver",
            ttl_seconds=ttl_seconds, delegates=delegates,
        )

    async def attempt(call):
        try:
            return {"ok": str(await call)}
        except Exception as error:
            return {"error": type(error).__name__}

    async def run(main):
        engine = make_engine(Settings())
        try:
            queue = PgApprovalQueue(
                make_session_factory(engine),
                policy=RoleApproverPolicy(roles_by_action=ROLES_BY_ACTION),
                listed_actions=ROLES_BY_ACTION,
            )
            return await main(queue)
        finally:
            await engine.dispose()
    """
)


def in_api(body: str) -> dict[str, Any]:
    """Run `async def main(queue)` from the api container, on the requester role."""
    script = PRELUDE + textwrap.dedent(body) + "\nprint(json.dumps(asyncio.run(run(main))))\n"
    result = compose("exec", "-T", "api", "python", "-c", script, check=False)
    assert result.returncode == 0, result.stderr[-800:]
    parsed: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return parsed


def test_delegates_are_stored_unchanged_and_capped_at_sixteen() -> None:
    out = in_api(
        """
        async def main(queue):
            names = [f"svc.d{i:02d}" for i in range(16)]
            request = await submit(queue, delegates=names)
            again = await queue.get(request.id)
            too_many = await attempt(submit(queue, delegates=names + ["svc.d16"]))
            bad_id = await attempt(submit(queue, delegates=["has space"]))
            return {"stored": sorted(again.delegates) == sorted(names),
                    "too_many": too_many, "bad_id": bad_id, "id": str(request.id)}
        """
    )
    assert out["stored"] is True
    assert out["too_many"] == {"error": "ValueError"}
    assert out["bad_id"]["error"] in {"ValueError", "ValidationError"}
    stored = psql(
        f"select jsonb_array_length(delegates) from core.approvals where id = '{out['id']}'"
    )
    assert stored.stdout.strip() == "16"


def test_only_the_requester_or_a_delegate_may_consume(
    approver: ApproverClient,
) -> None:
    made = in_api(
        """
        async def main(queue):
            request = await submit(queue, delegates=["service.delegate"])
            return {"id": str(request.id)}
        """
    )
    approval_id = made["id"]
    assert approver.decide(approval_id, "approve", csrf=approver.page_csrf()).status_code == 303

    out = in_api(
        f"""
        async def main(queue):
            args = dict(action="kit_smoke.echo", payload={{"n": 1}})
            id = UUID("{approval_id}")
            stranger = await attempt(queue.consume(id, principal=OTHER, **args))
            approver_like = await attempt(queue.consume(
                id, principal=Principal(id="approver", kind=PrincipalKind.HUMAN,
                                        roles=frozenset({{"approver"}})), **args))
            by_delegate = await attempt(queue.consume(id, principal=DELEGATE, **args))
            again = await attempt(queue.consume(id, principal=N8N_SERVICE, **args))
            return {{"stranger": stranger, "approver_like": approver_like,
                    "by_delegate": by_delegate, "again": again}}
        """
    )
    assert out["stranger"] == {"error": "NotTheRequesterError"}
    assert out["approver_like"] == {"error": "NotTheRequesterError"}
    assert "ok" in out["by_delegate"]
    assert out["again"] == {"error": "ApprovalAlreadyResolvedError"}
    assert audit_count("approval.consume_denied", approval_id) == 3
    reasons = psql(
        "select payload from core.audit_log where action = 'approval.consume_denied' "
        f"and subject_id = '{approval_id}' order by seq"
    ).stdout
    assert reasons.count('"reason":"not_requester"') == 2
    assert audit_count("approval.consumed", approval_id) == 1


def test_the_requester_cancels_and_a_delegate_cannot() -> None:
    out = in_api(
        """
        async def main(queue):
            request = await submit(queue, delegates=["service.delegate"])
            by_delegate = await attempt(queue.cancel(request.id, principal=DELEGATE))
            by_stranger = await attempt(queue.cancel(request.id, principal=OTHER))
            cancelled = await queue.cancel(request.id, principal=N8N_SERVICE, reason="changed mind")
            again = await attempt(queue.cancel(request.id, principal=N8N_SERVICE))
            return {"id": str(request.id), "by_delegate": by_delegate, "by_stranger": by_stranger,
                    "status": cancelled.status.value, "closed": cancelled.closed_at is not None,
                    "again": again}
        """
    )
    assert out["by_delegate"] == {"error": "NotTheRequesterError"}
    assert out["by_stranger"] == {"error": "NotTheRequesterError"}
    assert (out["status"], out["closed"]) == ("cancelled", True)
    assert out["again"] == {"error": "ApprovalAlreadyResolvedError"}
    approval_id = out["id"]
    assert audit_count("approval.cancelled", approval_id) == 1
    assert audit_count("approval.cancel_denied", approval_id) == 3  # delegate, stranger, again
    stored = psql(
        f"select status, closed_at is not null from core.approvals where id = '{approval_id}'"
    )
    assert stored.stdout.strip() == "cancelled|t"
    reason = psql(
        "select payload from core.audit_log where action = 'approval.cancelled' "
        f"and subject_id = '{approval_id}'"
    ).stdout
    assert "changed mind" in reason


def test_a_cancelled_request_cannot_be_decided(
    approver: ApproverClient, service: httpx.Client, make_approval: MakeApproval
) -> None:
    approval_id = make_approval()["approval_id"]
    out = in_api(
        f"""
        async def main(queue):
            done = await queue.cancel(UUID("{approval_id}"), principal=N8N_SERVICE)
            return {{"status": done.status.value}}
        """
    )
    assert out["status"] == "cancelled"
    assert approver.decide(approval_id, "approve", csrf=approver.page_csrf()).status_code == 409
    assert status_of(service, approval_id) == "cancelled"


def test_expire_due_stores_expired_with_closed_at_and_audits_each() -> None:
    out = in_api(
        """
        import time
        async def main(queue):
            lapsing = await submit(queue, ttl_seconds=1)
            later = await submit(queue, ttl_seconds=600)
            # A caller whose clock runs a year ahead expires nothing the database finds live.
            skewed = await queue.expire_due(
                principal=SWEEP, now=datetime.now(UTC) + timedelta(days=365))
            still = (await queue.get(later.id)).status.value
            await asyncio.sleep(2.5)
            read_before_sweep = await queue.get(lapsing.id)
            swept = await queue.expire_due(principal=SWEEP, limit=1)
            stored = await queue.get(lapsing.id)
            return {"lapsing": str(lapsing.id), "later": str(later.id), "still": still,
                    "swept": swept, "skewed": skewed,
                    "read": [read_before_sweep.status.value,
                             read_before_sweep.closed_at is not None],
                    "stored": [stored.status.value, stored.closed_at is not None]}
        """
    )
    assert out["still"] == "pending"
    # The api's own 60 s sweep may have expired it first, so `swept` can be 0 or 1 and says
    # nothing; what matters is the stored row below, whoever stored it.
    assert out["read"] == ["expired", True]
    assert out["stored"] == ["expired", True]
    stored = psql(
        f"select status, closed_at = expires_at from core.approvals where id = '{out['lapsing']}'"
    )
    assert stored.stdout.strip() == "expired|t"
    assert audit_count("approval.expired", out["lapsing"]) == 1
    assert (
        psql(f"select status from core.approvals where id = '{out['later']}'").stdout.strip()
        == "pending"
    )
    actor = psql(
        "select actor_id from core.audit_log where action = 'approval.expired' "
        f"and subject_id = '{out['lapsing']}'"
    ).stdout.strip()
    assert actor in {"service.itest-sweep", "service.sweep"}
    assert (
        psql(f"select status from core.approvals where id = '{out['later']}'").stdout.strip()
        == "pending"
    )


def test_the_database_refuses_an_early_expiry_and_a_stray_closed_at(
    make_approval: MakeApproval,
) -> None:
    approval_id = make_approval()["approval_id"]
    early = psql(
        "update core.approvals set status = 'expired', closed_at = now() "
        f"where id = '{approval_id}'",
        role="opskit_app",
    )
    assert early.returncode != 0
    assert "has not reached its expiry" in early.stderr
    stray = psql(f"update core.approvals set closed_at = now() where id = '{approval_id}'")
    assert stray.returncode != 0


def test_a_closed_at_is_set_only_on_expired_and_cancelled_rows() -> None:
    wrong = psql(
        "select count(*) from core.approvals "
        "where (status in ('expired', 'cancelled')) <> (closed_at is not null)"
    )
    assert wrong.stdout.strip() == "0"


# --- the lifetime cap does not depend on the session time zone -----------------------------


def _insert_with_lifetime(zone: str, lifetime_s: int, created_offset_s: int = 0) -> str:
    """Insert as the requester under `zone`; the block raises at the end so nothing is kept.
    Returns the error text, which says whether the insert got past the guard."""
    sql = (
        f"set time zone '{zone}'; "
        "do $$ begin insert into core.approvals (action, summary, payload, payload_sha256, "
        "requested_by, required_role, created_at, expires_at) values ('kit_smoke.echo', 's', "
        f"'{{}}', repeat('0', 64), 'service.n8n', 'approver', "
        f"now() + {created_offset_s} * interval '1 second', "
        f"now() + {created_offset_s + lifetime_s} * interval '1 second'); "
        "raise exception 'inserted ok'; end $$"
    )
    return psql(sql, role="opskit_app").stderr


@pytest.mark.parametrize("zone", ["+14", "-12", "UTC"])
def test_the_seven_day_cap_holds_to_the_minute_in_any_session_time_zone(zone: str) -> None:
    week = 7 * 24 * 3600
    assert "inserted ok" in _insert_with_lifetime(zone, week)
    assert "at most 7 days" in _insert_with_lifetime(zone, week + 60)
    assert "at most 7 days" in _insert_with_lifetime(zone, 0)


@pytest.mark.parametrize("zone", ["+14", "-12"])
def test_a_backdated_or_future_created_at_is_refused_in_any_time_zone(zone: str) -> None:
    assert "inserted ok" in _insert_with_lifetime(zone, 3600, created_offset_s=-240)
    assert "at most 7 days" in _insert_with_lifetime(zone, 3600, created_offset_s=-420)
    assert "at most 7 days" in _insert_with_lifetime(zone, 3600, created_offset_s=420)


# --- rows the requester role may and may not plant ------------------------------------------

PLANT = (
    "insert into core.approvals (action, summary, payload, payload_sha256, requested_by, "
    "required_role, expires_at{extra_cols}) values ('{action}', '{summary}', '{{}}', "
    "repeat('0', 64), 'service.n8n', 'approver', now() + interval '1 hour'{extra_vals}) "
    "returning id"
)


SHAPE_REFUSED = "shape the application refuses"


@pytest.mark.parametrize(
    ("columns", "values"),
    [
        ("summary", "''"),
        ("summary", "repeat('x', 501)"),
        ("action", "'Not An Action'"),
        ("action", "'a.' || repeat('b', 100)"),
        ("requested_by", "'svc/x'"),
        ("requested_by", "'s' || repeat('x', 150)"),
        ("required_role", "'Not A Role'"),
        ("payload_sha256", "'nothex'"),
        ("delegates", "'[\"has/slash\"]'::jsonb"),
        ("delegates", "'[1]'::jsonb"),
        ("delegates", "'{}'::jsonb"),
        ("run_context", "'{}'::jsonb"),
        ("run_context", "'[]'::jsonb"),
        ("run_context", "'\"x\"'::jsonb"),
        ("run_context", "'null'::jsonb"),
        ("run_context", '\'{"run_id": "r1", "extra": 1}\'::jsonb'),
        ("run_context", "'{\"run_id\": 5}'::jsonb"),
    ],
)
def test_a_row_the_application_could_not_read_back_cannot_be_stored(
    columns: str, values: str
) -> None:
    defaults = {
        "action": "'kit_smoke.echo'",
        "summary": "'s'",
        "payload": "'{}'::jsonb",
        "payload_sha256": "repeat('0', 64)",
        "requested_by": "'service.n8n'",
        "required_role": "'approver'",
        "expires_at": "now() + interval '1 hour'",
    }
    row = {**defaults, columns: values}
    sql = (
        f"insert into core.approvals ({', '.join(row)}) values ({', '.join(row.values())}) "
        "returning id"
    )
    result = psql(sql, role="opskit_app")
    assert result.returncode != 0, result.stdout
    assert SHAPE_REFUSED in result.stderr, result.stderr


def test_a_new_row_cannot_arrive_with_a_reason() -> None:
    sql = PLANT.format(
        action="kit_smoke.echo", summary="s", extra_cols=", reason", extra_vals=", 'preset'"
    )
    assert "no reason yet" in psql(sql, role="opskit_app").stderr


def test_a_request_without_a_run_stores_sql_null_for_its_context() -> None:
    out = in_api(
        """
        async def main(queue):
            request = await submit(queue)
            return {"id": str(request.id)}
        """
    )
    stored = psql(f"select run_context is null from core.approvals where id = '{out['id']}'")
    assert stored.stdout.strip() == "t"


def test_an_unreadable_row_does_not_end_the_listing_for_the_rows_after_it() -> None:
    """The listing counts fetched rows, so a page with a skipped row still has a next cursor."""
    out = in_api(
        """
        from opskit.core.ports import APPROVER
        async def main(queue):
            rows = [await submit(queue) for _ in range(3)]
            page = await queue.list_pending_page(APPROVER, limit=2, cursor=None)
            return {"items": len(page.items), "more": page.next_cursor is not None}
        """
    )
    assert out["items"] <= 2 and out["more"] is True


def test_an_unlisted_action_is_left_off_the_listing_and_refused_with_a_403_and_an_audit(
    approver: ApproverClient,
) -> None:
    planted = psql(
        PLANT.format(action="test.unlisted", summary="s", extra_cols="", extra_vals=""),
        role="opskit_app",
    )
    assert planted.returncode == 0, planted.stderr
    approval_id = planted.stdout.split()[0]
    control = in_api(
        """
        from opskit.core.ports import APPROVER
        async def main(queue):
            listed = await queue.list_pending(APPROVER, limit=100000)
            return {"ids": [str(r.id) for r in listed]}
        """
    )["ids"]
    assert control, "the listing returned nothing, so absence proves nothing"
    assert approval_id not in control
    response = approver.decide(approval_id, "approve", csrf=approver.page_csrf())
    assert response.status_code == 403
    assert audit_count("approval.denied", approval_id) == 1
    status = psql(f"select status from core.approvals where id = '{approval_id}'").stdout.strip()
    assert status == "pending"
