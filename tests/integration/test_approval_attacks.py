"""Every way I know to move an approval to approved or rejected without the approver path.

Each attempt must fail, and the request must still be pending afterwards. They run as the
requester role unless the test says otherwise; the owner and superuser rows document what
the guard does for them (a plain UPDATE is refused; they could still disable the trigger,
which is a documented limit, so that is not attempted here).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tests.integration.conftest import ApproverClient, audit_count, psql

pytestmark = pytest.mark.integration

MakeApproval = Callable[..., dict[str, Any]]

COLUMNS = "id, action, summary, payload, payload_sha256, requested_by, required_role, expires_at"
UPDATE_COLUMNS = "status = 'approved', decision = 'approve', resolved_by = 'x', resolved_at = now()"

ATTEMPTS: dict[str, tuple[str, str]] = {
    "on conflict do update": (
        f"insert into core.approvals ({COLUMNS}) select {COLUMNS} from core.approvals "
        "where id = '{id}' on conflict (id) do update set status = 'approved'",
        "",
    ),
    "on conflict do update, every column": (
        f"insert into core.approvals ({COLUMNS}) select {COLUMNS} from core.approvals "
        f"where id = '{{id}}' on conflict (id) do update set {UPDATE_COLUMNS}",
        "",
    ),
    "merge": (
        "merge into core.approvals t using (select id from core.approvals where id = '{id}') s "
        "on t.id = s.id when matched then update set status = 'approved'",
        "",
    ),
    "update from": (
        "update core.approvals t set status = 'approved' from (select '{id}'::uuid as id) s "
        "where t.id = s.id",
        "",
    ),
    "writable cte": (
        "with u as (update core.approvals set status = 'approved' where id = '{id}' "
        "returning 1) select * from u",
        "",
    ),
    "prepared statement": (
        "prepare p as update core.approvals set status = 'rejected' where id = '{id}'; execute p",
        "",
    ),
    "delete": ("delete from core.approvals where id = '{id}'", "permission denied"),
    "truncate": ("truncate core.approvals", "permission denied"),
    "delete then re-insert": (
        "delete from core.approvals where id = '{id}'; "
        f"insert into core.approvals ({COLUMNS}, status) values ('{{id}}', 'a', 's', '{{}}', "
        "repeat('0', 64), 'x', 'approver', now() + interval '1 hour', 'approved')",
        "permission denied",
    ),
    "set session authorization": (
        "set session authorization opskit_approver; update core.approvals set status = "
        "'approved' where id = '{id}'",
        "permission denied to set session authorization",
    ),
    "security definer function": (
        "create function core.evil() returns void language sql security definer as "
        "$$ update core.approvals set status = 'approved' $$",
        "permission denied",
    ),
    "temp function": (
        "create function pg_temp.evil() returns void language sql as $$ select 1 $$",
        "permission denied",
    ),
    "own trigger": (
        "create trigger evil before update on core.approvals for each row "
        "execute function core.approvals_guard()",
        "permission denied|must be owner",
    ),
    "disable the guard": (
        "alter table core.approvals disable trigger approvals_guard",
        "must be owner",
    ),
    "drop the guard": ("drop trigger approvals_guard on core.approvals", "must be owner"),
    "replace the guard function": (
        "create or replace function core.approvals_guard() returns trigger language plpgsql "
        "as $$ begin return new; end $$",
        "permission denied|must be owner",
    ),
    "grant itself the decision columns": (
        "grant update (decision) on core.approvals to opskit_app",
        "",
    ),
    "replica role": (
        "set session_replication_role = replica; update core.approvals set status = "
        "'approved' where id = '{id}'",
        "permission denied",
    ),
    "server file read": ("select pg_read_file('/etc/passwd')", "permission denied"),
    "copy from program": (
        "copy core.approvals from program 'true'",
        "permission denied",
    ),
    "large object import": ("select lo_import('/etc/passwd')", "permission denied"),
    "update through a rule": (
        "create rule evil as on update to core.approvals do instead nothing",
        "must be owner",
    ),
}


def _status(approval_id: str) -> str:
    return psql(f"select status from core.approvals where id = '{approval_id}'").stdout.strip()


@pytest.mark.parametrize("name", sorted(ATTEMPTS))
def test_the_requester_role_cannot_get_an_approval_decided(
    name: str, make_approval: Callable[..., dict[str, Any]]
) -> None:
    sql, message = ATTEMPTS[name]
    approval_id = make_approval()["approval_id"]
    result = psql(sql.replace("{id}", approval_id), role="opskit_app")
    if name == "grant itself the decision columns":
        # A non-owner GRANT warns and changes nothing; check the effect.
        check = "select has_column_privilege('opskit_app', 'core.approvals', 'decision', 'UPDATE')"
        assert psql(check).stdout.strip() == "f"
    else:
        assert result.returncode != 0, f"{name} was accepted: {result.stdout}"
        assert any(m in result.stderr for m in message.split("|")), result.stderr
    assert _status(approval_id) == "pending"


@pytest.mark.parametrize("role", ["opskit_owner", "postgres"])
@pytest.mark.parametrize("name", ["update from", "writable cte", "merge", "on conflict do update"])
def test_a_plain_statement_by_the_owner_or_a_superuser_is_refused_too(
    role: str, name: str, make_approval: Callable[..., dict[str, Any]]
) -> None:
    approval_id = make_approval()["approval_id"]
    result = psql(ATTEMPTS[name][0].replace("{id}", approval_id), role=role)
    assert result.returncode != 0
    # An upsert is refused at the insert half ("requester role may create") or the update half.
    assert any(
        text in result.stderr
        for text in ("only the approver role may approve", "only the requester role may create")
    ), result.stderr
    assert _status(approval_id) == "pending"


def test_the_requester_cannot_make_the_approver_page_show_other_than_what_is_covered(
    approver: ApproverClient,
) -> None:
    """The requester role writes both the payload shown and the hash a consume checks; the
    decision refuses a row where they disagree."""
    planted = psql(
        "insert into core.approvals (action, summary, payload, payload_sha256, requested_by, "
        "required_role, expires_at) values ('kit_smoke.echo', 's', '{\"shown\": 1}', "
        "repeat('0', 64), 'service.n8n', 'approver', now() + interval '1 hour') returning id",
        role="opskit_app",
    )
    assert planted.returncode == 0, planted.stderr
    approval_id = planted.stdout.split()[0]
    response = approver.decide(approval_id, "approve", csrf=approver.page_csrf())
    assert response.status_code == 403
    assert audit_count("approval.denied", approval_id) == 1
    reason = psql(
        "select payload from core.audit_log where action = 'approval.denied' "
        f"and subject_id = '{approval_id}'"
    ).stdout
    assert "payload_mismatch" in reason
    assert _status(approval_id) == "pending"


def test_the_approver_role_gains_no_membership_and_the_requester_none() -> None:
    members = psql(
        "select count(*) from pg_auth_members m join pg_roles r on r.oid in (m.roleid, m.member) "
        "where r.rolname in ('opskit_app', 'opskit_approver')"
    )
    assert members.stdout.strip() == "0"
