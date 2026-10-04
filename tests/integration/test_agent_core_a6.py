"""Phase 3e: what agent-core v0.1.0a4 to a6 added, held by the database and the queue.

Each rule is tested where it is enforced: through the queue and audit log from the api container
(the requester and approver roles, as the api uses them) and, for the rules the database owns,
straight at the database as the role under test, so they hold even if the code is bypassed.
"""

from __future__ import annotations

import json
import secrets
import textwrap
from typing import Any
from uuid import uuid4

import httpx
import pytest

from opskit.db.migrations.versions import core_0012_approvals_a6 as migration
from tests.integration.conftest import (
    N8N_PORT,
    ApproverClient,
    age_approval,
    audit_count,
    compose,
    psql,
)
from tests.integration.test_inbox_drafts import _draft_status, _take_pending_draft

pytestmark = pytest.mark.integration

PRELUDE = textwrap.dedent(
    """
    import asyncio, json
    from datetime import UTC, datetime, timedelta
    from uuid import UUID, uuid4
    from aox_agent_core.approvals import RoleApproverPolicy
    from aox_agent_core.audit import AuditEvent, compute_record_hash
    from opskit.config import Settings
    from opskit.core.errors import (
        ApprovalConflictError, ApprovalIntegrityError, ApprovalPayloadPurgedError,
    )
    from opskit.core.pg.approvals import PgApprovalQueue
    from opskit.core.pg.audit import PgAuditLog
    from opskit.core.ports import N8N_SERVICE, ROLES_BY_ACTION, Principal, PrincipalKind
    from opskit.db.engine import make_approver_engine, make_engine, make_session_factory

    HUMAN = Principal(id="approver", kind=PrincipalKind.HUMAN, roles=frozenset({"approver"}))
    NONCE = "__NONCE__"

    async def submit(queue, *, nonce=NONCE, summary="itest", role="approver", ttl=600,
                     delegates=(), include=False, resume_url=None):
        return await queue.submit(
            action="kit_smoke.echo", summary=summary, payload={"n": 1, "nonce": nonce},
            requested_by=N8N_SERVICE, required_role=role, ttl_seconds=ttl,
            delegates=delegates, include_payload=include, resume_url=resume_url,
        )

    async def attempt(call):
        try:
            return {"ok": str(await call)}
        except Exception as error:
            return {"error": type(error).__name__, "text": str(error)[:200]}

    async def run(main):
        settings = Settings()
        engine, approver_engine = make_engine(settings), make_approver_engine(settings)
        try:
            factory = make_session_factory(engine)
            queue = PgApprovalQueue(
                factory,
                policy=RoleApproverPolicy(roles_by_action=ROLES_BY_ACTION),
                listed_actions=ROLES_BY_ACTION,
                approver_session_factory=make_session_factory(approver_engine),
            )
            return await main(queue, PgAuditLog(factory))
        finally:
            await approver_engine.dispose()
            await engine.dispose()
    """
)


def in_api(body: str, nonce: str | None = None) -> dict[str, Any]:
    """Run `async def main(queue, audit)` from the api container, on the api's own roles."""
    script = PRELUDE.replace("__NONCE__", nonce or uuid4().hex)
    script += textwrap.dedent(body) + "\nprint(json.dumps(asyncio.run(run(main))))\n"
    result = compose("exec", "-T", "api", "python", "-c", script, check=False)
    assert result.returncode == 0, result.stderr[-1200:]
    parsed: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return parsed


def one(sql: str, *, role: str = "postgres") -> str:
    result = psql(sql, role=role)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[0]


def backdate_finish(approval_id: str, column: str = "closed_at") -> None:
    """Make a finished request old, as only the table owner can (guard off for one statement)."""
    result = psql(
        "alter table core.approvals disable trigger approvals_guard; "
        f"update core.approvals set {column} = now() - interval '2 days' "
        f"where id = '{approval_id}'; "
        "alter table core.approvals enable always trigger approvals_guard"
    )
    assert result.returncode == 0, result.stderr


# --- audit: append_many, occurred_at, recorded_at --------------------------------------------


def test_append_many_is_ordered_gapless_and_all_or_nothing() -> None:
    out = in_api(
        """
        async def main(queue, audit):
            before = await audit.head()
            events = [
                AuditEvent(action="itest.batch", actor_id="itest", subject_id=f"b{i}",
                           payload={"i": i})
                for i in range(5)
            ]
            records = await audit.append_many(events)
            after = await audit.head()
            late = AuditEvent(action="itest.batch", actor_id="itest", payload={"late": 1},
                              occurred_at=datetime.now(UTC) + timedelta(hours=1))
            refused = await attempt(audit.append_many([*events[:2], late]))
            after_refusal = await audit.head()
            too_many = await attempt(audit.append_many(events * 201))
            return {
                "first": before.seq + 1,
                "seqs": [r.seq for r in records],
                "subjects": [r.subject_id for r in records],
                "linked": all(
                    b.prev_hash == a.record_hash for a, b in zip(records, records[1:])
                ),
                "head_is_last": after.seq == records[-1].seq
                    and after.record_hash == records[-1].record_hash,
                "refused": refused,
                "unchanged_after_refusal": after_refusal == after,
                "too_many": too_many,
                "empty": await audit.append_many([]),
                "verified_to": (await audit.verify()).seq,
            }
        """
    )
    assert out["seqs"] == list(range(out["first"], out["first"] + 5))
    assert out["subjects"] == [f"b{i}" for i in range(5)]
    assert out["linked"] and out["head_is_last"]
    assert out["refused"]["error"] == "AuditTimeRejectedError"
    assert "Event 2" in out["refused"]["text"]
    assert out["unchanged_after_refusal"] is True
    assert out["too_many"]["error"] == "ValueError"
    assert out["empty"] == []
    assert out["verified_to"] >= out["seqs"][-1]


def test_the_hash_covers_occurred_at_and_nothing_the_database_adds() -> None:
    out = in_api(
        """
        async def main(queue, audit):
            then = datetime.now(UTC) - timedelta(hours=1)
            record = (await audit.append_many([
                AuditEvent(action="itest.timed", actor_id="itest", payload={}, occurred_at=then)
            ]))[0]
            other_time = record.model_copy(update={"occurred_at": then - timedelta(seconds=1)})
            other_stamps = record.model_copy(update={
                "recorded_at": record.recorded_at - timedelta(days=3), "db_role": "somebody_else",
            })
            return {
                "supplied_time_kept": record.occurred_at == then,
                "stamped": record.recorded_at is not None and record.db_role is not None,
                "hash_ok": compute_record_hash(record) == record.record_hash,
                "occurred_at_is_hashed": compute_record_hash(other_time) != record.record_hash,
                "recorded_at_and_role_are_not": compute_record_hash(other_stamps)
                    == record.record_hash,
                "chain_verifies": (await audit.verify()).seq >= record.seq,
            }
        """
    )
    assert all(out.values()), out


def test_an_unsupplied_occurred_at_is_the_database_clock() -> None:
    out = in_api(
        """
        async def main(queue, audit):
            record = await audit.append(
                AuditEvent(action="itest.clock", actor_id="itest", payload={})
            )
            skew = abs((record.occurred_at - record.recorded_at).total_seconds())
            return {"skew_ms": skew * 1000}
        """
    )
    assert out["skew_ms"] < 2000


INSERT_AUDIT = (
    "begin; insert into core.audit_log (seq, schema_version, event_id, occurred_at, action, "
    "actor_id, payload, prev_hash, record_hash{extra_cols}) values "
    "((select coalesce(max(seq), 0) + 1 from core.audit_log), 3, gen_random_uuid(), {occurred}, "
    "'itest.append', 'itest', '{{}}', repeat('0', 64), repeat(md5(random()::text), 2)"
    "{extra_vals}) returning {returning}; rollback;"
)


@pytest.mark.parametrize("role", ["opskit_app", "opskit_approver"])
@pytest.mark.parametrize(
    "occurred",
    [
        "now() + interval '10 minutes'",
        "now() - interval '25 hours'",
        "now() + interval '1 day'",
        "'infinity'::timestamptz",
    ],
)
def test_occurred_at_outside_the_window_is_refused_by_the_database(
    role: str, occurred: str
) -> None:
    sql = INSERT_AUDIT.format(occurred=occurred, extra_cols="", extra_vals="", returning="seq")
    result = psql(sql, role=role)
    assert result.returncode != 0
    assert "occurred_at must be within" in result.stderr


@pytest.mark.parametrize(
    "occurred", ["now() + interval '2 minutes'", "now() - interval '23 hours'"]
)
def test_occurred_at_inside_the_window_is_accepted(occurred: str) -> None:
    sql = INSERT_AUDIT.format(occurred=occurred, extra_cols="", extra_vals="", returning="seq")
    assert psql(sql, role="opskit_app").returncode == 0


def test_the_database_sets_recorded_at_whatever_the_writer_sends() -> None:
    sql = INSERT_AUDIT.format(
        occurred="now()",
        extra_cols=", recorded_at, db_role",
        extra_vals=", '2000-01-01', 'somebody_else'",
        returning="recorded_at > now() - interval '1 minute', db_role",
    )
    result = psql(sql, role="opskit_app")
    assert result.returncode == 0, result.stderr
    assert "t|opskit_app" in result.stdout.splitlines()


# --- approvals: idempotent submit --------------------------------------------------------------


def test_an_exact_repeat_returns_the_open_request_and_writes_nothing() -> None:
    out = in_api(
        """
        async def main(queue, audit):
            first = await submit(queue, delegates=["service.b", "service.a"])
            again = await submit(queue, delegates=["service.a", "service.b"], include=True)
            return {"first": str(first.id), "again": str(again.id),
                    "payload_returned": again.payload is not None,
                    "no_payload_unless_asked": first.payload is None}
        """
    )
    assert out["first"] == out["again"]
    assert out["payload_returned"] and out["no_payload_unless_asked"]
    assert audit_count("approval.requested", out["first"]) == 1
    assert one(f"select count(*) from core.approvals where id = '{out['first']}'") == "1"


def test_a_repeat_on_other_terms_is_a_conflict_that_names_what_differs() -> None:
    out = in_api(
        """
        async def main(queue, audit):
            first = await submit(queue, delegates=["service.a"])
            seen = {}
            for name, kwargs in {
                "summary": {"summary": "something else", "delegates": ["service.a"]},
                "required_role": {"role": "someone.else", "delegates": ["service.a"]},
                "lifetime": {"ttl": 601, "delegates": ["service.a"]},
                "delegates": {"delegates": ["service.a", "service.z"]},
                "resume_url": {
                    "resume_url": "http://api.example/resume",
                    "delegates": ["service.a"],
                },
            }.items():
                try:
                    await submit(queue, **kwargs)
                    seen[name] = "no error"
                except ApprovalConflictError as error:
                    seen[name] = {"existing": str(error.existing), "differs": list(error.differs)}
            return {"id": str(first.id), "seen": seen}
        """
    )
    for name, got in out["seen"].items():
        assert got == {"existing": out["id"], "differs": [name]}, (name, got)
    assert audit_count("approval.submit_conflict", out["id"]) == 5
    assert audit_count("approval.requested", out["id"]) == 1


def test_racing_identical_submits_share_one_request() -> None:
    out = in_api(
        """
        async def main(queue, audit):
            results = await asyncio.gather(*[submit(queue) for _ in range(6)])
            return {"ids": sorted({str(r.id) for r in results})}
        """
    )
    assert len(out["ids"]) == 1
    assert one(f"select count(*) from core.approvals where id = '{out['ids'][0]}'") == "1"


def test_the_database_allows_one_open_request_per_key_even_for_plain_sql() -> None:
    made = in_api(
        "async def main(queue, audit):\n    return {'id': str((await submit(queue)).id)}\n"
    )
    columns = "action, summary, payload, payload_sha256, requested_by, required_role, expires_at"
    result = psql(
        f"insert into core.approvals ({columns}) select {columns} from core.approvals "
        f"where id = '{made['id']}'",
        role="opskit_app",
    )
    assert result.returncode != 0
    assert "approvals_one_open" in result.stderr


def test_a_closed_request_frees_its_key() -> None:
    nonce = uuid4().hex
    first = in_api(
        "async def main(queue, audit):\n    return {'id': str((await submit(queue)).id)}\n", nonce
    )
    in_api(
        f"""
        async def main(queue, audit):
            await queue.cancel(UUID("{first["id"]}"), principal=N8N_SERVICE)
            return {{}}
        """,
        nonce,
    )
    second = in_api(
        "async def main(queue, audit):\n    return {'id': str((await submit(queue)).id)}\n", nonce
    )
    assert second["id"] != first["id"]


def test_an_n8n_retry_of_the_same_draft_approval_returns_the_existing_request(
    service: httpx.Client,
) -> None:
    draft_id, approval_id, _ = _take_pending_draft()
    key = (
        "select count(*) from core.approvals where requested_by = 'service.n8n' "
        "and action = 'inbox.send_reply' and status in ('pending', 'approved') and "
        f"payload_sha256 = (select payload_sha256 from core.approvals where id = '{approval_id}')"
    )
    assert one(key) == "1"
    run = service.post(
        "/v1/runs", json={"workflow": "inbox", "n8n_execution_id": f"retry-{uuid4()}"}
    )
    assert run.status_code == 201, run.text
    url = one(f"select resume_url from core.approvals where id = '{approval_id}'")
    retried = service.post(
        f"/v1/inbox/drafts/{draft_id}/approval",
        json={"run_id": run.json()["run_id"], "resume_url": url},
    )
    assert retried.status_code == 201, retried.text
    assert retried.json()["approval_id"] == approval_id
    assert _draft_status(draft_id) == "pending"
    assert one(key) == "1"
    assert audit_count("approval.requested", approval_id) == 1

    # A retried execution carries a new resume URL; handing it the old approval would resume the
    # dead execution, so it is refused, naming the approval that stands.
    new_execution = service.post(
        f"/v1/inbox/drafts/{draft_id}/approval",
        json={
            "run_id": run.json()["run_id"],
            "resume_url": (
                f"http://localhost:{N8N_PORT}/webhook-waiting/{secrets.randbelow(10**9) + 10**8}"
                f"?signature={secrets.token_hex(32)}"
            ),
        },
    )
    assert new_execution.status_code == 409, new_execution.text
    assert new_execution.json()["existing"] == approval_id
    assert new_execution.json()["differs"] == ["resume_url"]
    assert one(key) == "1"


# --- approvals: the delegate rule ---------------------------------------------------------------


def test_a_delegate_cannot_approve_even_with_direct_sql() -> None:
    made = in_api(
        "async def main(queue, audit):\n"
        "    return {'id': str((await submit(queue, delegates=['service.delegate'])).id)}\n"
    )
    approval_id = made["id"]
    decide = (
        "update core.approvals set status = 'approved', decision = 'approve', "
        "resolved_by = '{who}', resolved_at = now() where id = '{id}'"
    )
    refused = psql(decide.format(who="service.delegate", id=approval_id), role="opskit_approver")
    assert refused.returncode != 0
    assert "delegate" in refused.stderr
    assert one(f"select status from core.approvals where id = '{approval_id}'") == "pending"
    # The same statement for a stranger is the ordinary decision, so the refusal is the rule's.
    allowed = psql(decide.format(who="a.person", id=approval_id), role="opskit_approver")
    assert allowed.returncode == 0, allowed.stderr


def test_a_delegate_is_not_offered_the_request_and_cannot_decide_it(
    approver: ApproverClient,
) -> None:
    made = in_api(
        "async def main(queue, audit):\n"
        "    return {'id': str((await submit(queue, delegates=['approver'])).id)}\n"
    )
    approval_id = made["id"]
    listed = in_api(
        f"""
        async def main(queue, audit):
            page = await queue.list_pending_page(HUMAN, limit=500)
            return {{"offered": "{approval_id}" in {{str(r.id) for r in page.items}}}}
        """
    )
    assert listed["offered"] is False
    response = approver.decide(approval_id, "approve", csrf=approver.page_csrf())
    assert response.status_code == 403
    assert one(f"select status from core.approvals where id = '{approval_id}'") == "pending"
    assert audit_count("approval.denied", approval_id) >= 1


# --- approvals: an approval that lapses unused ---------------------------------------------------


def test_an_approval_that_lapses_unused_expires_with_its_decision_and_frees_its_key() -> None:
    nonce = uuid4().hex
    made = in_api(
        "async def main(queue, audit):\n    return {'id': str((await submit(queue)).id)}\n", nonce
    )
    approval_id = made["id"]
    early = psql(
        "update core.approvals set status = 'expired', closed_at = now() "
        f"where id = '{approval_id}'",
        role="opskit_app",
    )
    assert early.returncode != 0  # pending and not yet due
    approved = psql(
        "update core.approvals set status = 'approved', decision = 'approve', "
        f"resolved_by = 'a.person', resolved_at = now() where id = '{approval_id}'",
        role="opskit_approver",
    )
    assert approved.returncode == 0, approved.stderr
    not_due = psql(
        f"update core.approvals set status = 'expired' where id = '{approval_id}'",
        role="opskit_app",
    )
    assert not_due.returncode != 0
    assert "not reached its expiry" in not_due.stderr

    age_approval(approval_id)
    out = in_api(
        f"""
        async def main(queue, audit):
            read = await queue.get(UUID("{approval_id}"))
            before = {{"status": read.status.value, "decision": read.decision.value}}
            swept = await queue.expire_due(principal=N8N_SERVICE)
            stored = await queue.get(UUID("{approval_id}"))
            fresh = await submit(queue)
            return {{"before_sweep": before, "swept": swept, "stored": stored.status.value,
                     "decision": stored.decision.value, "resolved_by": stored.resolved_by,
                     "closed_at_is_expires_at": stored.closed_at == stored.expires_at,
                     "fresh": str(fresh.id)}}
        """,
        nonce,
    )
    assert out["before_sweep"] == {"status": "expired", "decision": "approve"}
    assert out["swept"] >= 1
    assert out["stored"] == "expired" and out["decision"] == "approve"
    assert out["resolved_by"] == "a.person" and out["closed_at_is_expires_at"] is True
    assert out["fresh"] != approval_id  # the lapsed approval no longer holds the key
    assert audit_count("approval.expired", approval_id) == 1
    previous = one(
        "select payload::json ->> 'previous_status' from core.audit_log "
        f"where action = 'approval.expired' and subject_id = '{approval_id}'"
    )
    assert previous == "approved"


# --- approvals: payloads are verified on every read ---------------------------------------------


def test_a_stored_payload_that_is_not_the_one_hashed_is_never_shown_or_decided(
    service: httpx.Client, approver: ApproverClient
) -> None:
    nonce = uuid4().hex
    made = in_api(
        "async def main(queue, audit):\n    return {'id': str((await submit(queue)).id)}\n", nonce
    )
    approval_id = made["id"]
    assert service.get(f"/v1/approvals/{approval_id}").status_code == 200
    tampered = psql(
        "alter table core.approvals disable trigger approvals_guard; "
        "update core.approvals set payload = '{\"n\": 2}'::jsonb "
        f"where id = '{approval_id}'; "
        "alter table core.approvals enable always trigger approvals_guard"
    )
    assert tampered.returncode == 0, tampered.stderr

    assert service.get(f"/v1/approvals/{approval_id}").status_code == 409
    out = in_api(
        f"""
        async def main(queue, audit):
            id = UUID("{approval_id}")
            page = await queue.list_pending_page(HUMAN, limit=500)
            closed = await queue.close_pending(id, principal=N8N_SERVICE)
            return {{
                "get": await attempt(queue.get(id)),
                "unverified_get": (await queue.get(id, verify_payload=False)).status.value,
                "payload_of": await attempt(queue.payload_of(id)),
                "listed": str(id) in {{str(r.id) for r in page.items}},
                "closed": closed.status.value,
            }}
        """,
        nonce,
    )
    assert out["get"]["error"] == "ApprovalIntegrityError"
    assert out["payload_of"]["error"] == "ApprovalIntegrityError"
    assert out["listed"] is False
    assert out["unverified_get"] == "pending"  # what closing a draft reads, payload untouched
    assert out["closed"] == "cancelled"  # a requester can always withdraw

    second = in_api(
        "async def main(queue, audit):\n    return {'id': str((await submit(queue)).id)}\n"
    )["id"]
    psql(
        "alter table core.approvals disable trigger approvals_guard; "
        "update core.approvals set payload = '{\"n\": 3}'::jsonb "
        f"where id = '{second}'; alter table core.approvals enable always trigger approvals_guard"
    )
    response = approver.decide(second, "approve", csrf=approver.page_csrf())
    assert response.status_code == 403
    assert one(f"select status from core.approvals where id = '{second}'") == "pending"


# --- approvals: purging payloads ----------------------------------------------------------------


def _cancelled_request() -> str:
    request_id = in_api(
        "async def main(queue, audit):\n"
        "    request = await submit(queue)\n"
        "    await queue.cancel(request.id, principal=N8N_SERVICE)\n"
        "    return {'id': str(request.id)}\n"
    )["id"]
    return request_id


def test_purge_removes_the_payload_stamps_the_database_clock_and_keeps_the_hash() -> None:
    request_id = _cancelled_request()
    sha = one(f"select payload_sha256 from core.approvals where id = '{request_id}'")
    backdate_finish(request_id)
    out = in_api(
        f"""
        async def main(queue, audit):
            id = UUID("{request_id}")
            purged = await queue.purge_payloads(
                principal=N8N_SERVICE, older_than=timedelta(hours=24)
            )
            read = await queue.get(id)
            return {{
                "purged": purged,
                "payload": read.payload,
                "purged_at_set": read.payload_purged_at is not None,
                "purged_just_now":
                    (datetime.now(UTC) - read.payload_purged_at).total_seconds() < 120,
                "sha": read.payload_sha256,
                "payload_of": await attempt(queue.payload_of(id)),
            }}
        """
    )
    assert out["purged"] >= 1
    assert out["payload"] is None and out["purged_at_set"] and out["purged_just_now"]
    assert out["sha"] == sha
    assert out["payload_of"]["error"] == "ApprovalPayloadPurgedError"
    assert one(f"select payload is null from core.approvals where id = '{request_id}'") == "t"
    assert audit_count("approval.payload_purged", request_id) == 1


def test_the_database_stamps_payload_purged_at_whatever_the_statement_says() -> None:
    request_id = _cancelled_request()
    backdate_finish(request_id)
    result = psql(
        "update core.approvals set payload = null, payload_purged_at = '2000-01-01' "
        f"where id = '{request_id}'",
        role="opskit_approver",
    )
    assert result.returncode == 0, result.stderr
    stamped = one(
        "select payload_purged_at > now() - interval '1 minute' from core.approvals "
        f"where id = '{request_id}'"
    )
    assert stamped == "t"


def test_a_purge_cannot_be_triggered_early_by_a_backdated_time() -> None:
    request_id = _cancelled_request()
    # The requester cancelling with a backdated closed_at gets the database clock instead.
    pending = in_api(
        "async def main(queue, audit):\n    return {'id': str((await submit(queue)).id)}\n"
    )["id"]
    cancel = psql(
        "update core.approvals set status = 'cancelled', closed_at = now() - interval '3 days' "
        f"where id = '{pending}'",
        role="opskit_app",
    )
    assert cancel.returncode == 0, cancel.stderr
    assert (
        one(
            "select closed_at > now() - interval '1 minute' from core.approvals "
            f"where id = '{pending}'"
        )
        == "t"
    )

    for target in (request_id, pending):
        for purged_at in ("now()", "'2000-01-01'"):
            early = psql(
                f"update core.approvals set payload = null, payload_purged_at = {purged_at} "
                f"where id = '{target}'",
                role="opskit_approver",
            )
            assert early.returncode != 0
            assert "retention floor" in early.stderr
            assert (
                one(f"select payload is not null from core.approvals where id = '{target}'") == "t"
            )

    out = in_api(
        """
        async def main(queue, audit):
            short = await attempt(
                queue.purge_payloads(principal=N8N_SERVICE, older_than=timedelta(hours=1))
            )
            await queue.purge_payloads(principal=N8N_SERVICE, older_than=timedelta(hours=24))
            return {"short": short}
        """
    )
    assert out["short"]["error"] == "ValueError"
    for target in (request_id, pending):
        assert one(f"select payload is not null from core.approvals where id = '{target}'") == "t"


def test_only_a_finished_request_may_be_purged_and_only_by_the_approver_role() -> None:
    pending = in_api(
        "async def main(queue, audit):\n    return {'id': str((await submit(queue)).id)}\n"
    )["id"]
    unfinished = psql(
        "update core.approvals set payload = null, payload_purged_at = now() "
        f"where id = '{pending}'",
        role="opskit_approver",
    )
    assert unfinished.returncode != 0
    assert "finished" in unfinished.stderr

    finished = _cancelled_request()
    backdate_finish(finished)
    requester = psql(
        "update core.approvals set payload = null, payload_purged_at = now() "
        f"where id = '{finished}'",
        role="opskit_app",
    )
    assert requester.returncode != 0
    assert "permission denied" in requester.stderr
    # Not even the approver may restore a payload or clear the mark.
    assert one(f"select payload is not null from core.approvals where id = '{finished}'") == "t"
    psql(
        "update core.approvals set payload = null, payload_purged_at = now() "
        f"where id = '{finished}'",
        role="opskit_approver",
    )
    restore = psql(
        f"update core.approvals set payload = '{{}}'::jsonb, payload_purged_at = null "
        f"where id = '{finished}'",
        role="opskit_approver",
    )
    assert restore.returncode != 0


# --- the core_0012 duplicate step, on a database that holds what 3d allowed -------------------


def _plant(tag: str, n: int, status: str, *, lapsed: bool) -> str:
    """One open row for the key `tag`, planted with the guards off (as 3d could have left it)."""
    approved = status == "approved"
    decided = (
        "'approve', 'a.person', now() - interval '2 hours'" if approved else "null, null, null"
    )
    return (
        "insert into core.approvals (action, summary, payload, payload_sha256, requested_by, "
        "required_role, created_at, expires_at, status, decision, resolved_by, resolved_at) "
        f"values ('kit_smoke.echo', '{tag}-{n}', '{{}}', repeat(md5('{tag}'), 2), 'service.mig', "
        f"'approver', now() - interval '3 hours' + {n} * interval '1 second', "
        f"now() {'-' if lapsed else '+'} interval '1 hour', '{status}', {decided}); "
    )


def _run_duplicate_step(plants: list[str]) -> Any:
    """The migration's own SQL against planted rows, in a transaction that is rolled back."""
    sql = (
        "begin; drop index core.approvals_one_open; "
        "alter table core.approvals disable trigger approvals_bounds; "
        "alter table core.approvals disable trigger approvals_guard; "
        + "".join(plants)
        + migration.CLOSE_DUPLICATES
        + " select summary || '=' || status from core.approvals "
        "where requested_by = 'service.mig' order by summary; rollback;"
    )
    return psql(sql)


def test_the_migration_expires_lapsed_open_rows_instead_of_refusing_on_them() -> None:
    result = _run_duplicate_step(
        [
            # Two approved requests that were never used and have long lapsed: 3d kept them
            # approved for ever, and they must not be taken for a live conflict.
            _plant("A", 1, "approved", lapsed=True),
            _plant("A", 2, "approved", lapsed=True),
            # A lapsed approval beside two live pending requests: the oldest live one stands.
            _plant("B", 1, "approved", lapsed=True),
            _plant("B", 2, "pending", lapsed=False),
            _plant("B", 3, "pending", lapsed=False),
        ]
    )
    assert result.returncode == 0, result.stderr
    stored = [line for line in result.stdout.splitlines() if "=" in line]
    assert stored == ["A-1=expired", "A-2=expired", "B-1=expired", "B-2=pending", "B-3=cancelled"]
    assert "lapsed open request(s) expired" in result.stderr


def test_the_migration_still_refuses_two_live_approved_requests_and_says_what_to_do() -> None:
    result = _run_duplicate_step(
        [_plant("C", 1, "approved", lapsed=False), _plant("C", 2, "approved", lapsed=False)]
    )
    assert result.returncode != 0
    assert "live approved requests share" in result.stderr
    assert "disable approvals_guard" in result.stderr
