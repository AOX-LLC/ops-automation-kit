"""Inbox drafts against the running stack, after the inbox workflow has drafted the samples.

Each draft waits in its own n8n execution; these tests decide drafts on the approver page,
let n8n release and send, and check that only what was approved is ever sent, once.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from email import policy
from email.parser import BytesParser

import httpx
import pytest

from tests.integration.conftest import (
    N8N_PORT,
    REPO_ROOT,
    ApproverClient,
    age_approval,
    audit_count,
    compose,
    psql,
)

pytestmark = pytest.mark.integration

MESSAGES = REPO_ROOT / "samples" / "inbox" / "messages"
MAILPIT = f"http://127.0.0.1:{os.environ.get('KIT_MAILPIT_WEB_PORT', '4303')}"


def _header(file: str, name: str) -> str:
    message = BytesParser(policy=policy.default).parsebytes((MESSAGES / file).read_bytes())
    return str(message[name] or "")


def _message_id(file: str) -> str:
    return _header(file, "Message-ID").strip().strip("<>")


def _sql(query: str) -> str:
    result = psql(query)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _wait(predicate: Callable[[], bool], what: str, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(2)
    pytest.fail(f"timed out waiting for {what}")


# Re-store a message's triage through the real store, as a second, overlapping run would.
_RETRIAGE = """
import asyncio, sys
from uuid import UUID
from opskit.config import Settings
from opskit.db.engine import make_engine, make_session_factory
from opskit.inbox import store

async def main(message_id, quarantined):
    engine = make_engine(Settings())
    factory = make_session_factory(engine)
    current = await store.load_triage(factory, message_id)
    run_id = UUID(sys.argv[3])
    other = current.model_copy(update={
        "quarantined": quarantined,
        "route": "quarantine" if quarantined else "draft",
        "injection_reasons": [] if not quarantined else current.injection_reasons,
    })
    await store.save_triage(factory, run_id, other)
    await engine.dispose()

asyncio.run(main(sys.argv[1], sys.argv[2] == "held"))
"""


def _retriage(message_id: str, outcome: str) -> None:
    run_id = _sql(f"select run_id from inbox.triage where message_id = '{message_id}'")
    result = compose(
        "exec", "-T", "api", "python", "-c", _RETRIAGE, message_id, outcome, run_id, check=False
    )
    assert result.returncode == 0, result.stderr


def _take_pending_draft() -> tuple[str, str, str]:
    """A draft whose approval n8n has requested and is waiting on: (draft_id, approval_id, to)."""
    row = _sql(
        "select d.id, d.approval_id, d.to_addr from inbox.drafts d "
        "join core.approvals a on a.id = d.approval_id "
        "where d.status = 'pending' and a.status = 'pending' and a.expires_at > now() "
        "order by d.created_at limit 1"
    )
    if not row:
        pytest.skip("no pending draft left to use")
    draft_id, approval_id, to = row.split("|")
    return draft_id, approval_id, to


def _draft_status(draft_id: str) -> str:
    return _sql(f"select status from inbox.drafts where id = '{draft_id}'")


def _replies_to(address: str) -> int:
    response = httpx.get(
        f"{MAILPIT}/api/v1/search",
        params={"query": f'from:"inbox@kit.example" to:"{address}"', "limit": 1},
        timeout=10,
    )
    response.raise_for_status()
    return int(response.json()["messages_count"])


def test_a_draft_changed_after_approval_is_never_sent(approver: ApproverClient) -> None:
    draft_id, approval_id, to = _take_pending_draft()
    before = _replies_to(to)
    # Simulate tampering between approval and sending: the stored draft no longer matches.
    _sql(f"update inbox.drafts set body = body || ' P.S. tampered' where id = '{draft_id}'")
    assert approver.decide(approval_id, "approve", csrf=approver.page_csrf()).status_code == 303

    _wait(lambda: audit_count("approval.consume_denied", approval_id) >= 1, "the refused release")
    time.sleep(3)
    assert _draft_status(draft_id) == "pending"
    assert _replies_to(to) == before
    reasons = _sql(
        "select payload from core.audit_log where action = 'approval.consume_denied' "
        f"and subject_id = '{approval_id}'"
    )
    assert "payload_mismatch" in reasons


def test_an_approved_draft_is_sent_once(approver: ApproverClient, service: httpx.Client) -> None:
    draft_id, approval_id, to = _take_pending_draft()
    before = _replies_to(to)
    assert approver.decide(approval_id, "approve", csrf=approver.page_csrf()).status_code == 303

    _wait(lambda: _draft_status(draft_id) == "sent", "the draft to be sent")
    _wait(lambda: _replies_to(to) == before + 1, "the reply in Mailpit")
    again = service.post(f"/v1/inbox/drafts/{draft_id}/release")
    assert again.status_code == 409
    assert _replies_to(to) == before + 1


def test_a_rejected_draft_stays_unsent(approver: ApproverClient, service: httpx.Client) -> None:
    draft_id, approval_id, to = _take_pending_draft()
    before = _replies_to(to)
    assert approver.decide(approval_id, "reject", csrf=approver.page_csrf()).status_code == 303

    _wait(lambda: _draft_status(draft_id) == "rejected", "the draft to be closed as rejected")
    assert service.post(f"/v1/inbox/drafts/{draft_id}/release").status_code == 409
    assert _replies_to(to) == before
    assert _sql(f"select sent_at is null from inbox.drafts where id = '{draft_id}'") == "t"


def test_an_expired_approval_closes_the_draft_as_expired(
    approver: ApproverClient, service: httpx.Client
) -> None:
    draft_id, approval_id, _ = _take_pending_draft()
    age_approval(approval_id)

    assert service.post(f"/v1/inbox/drafts/{draft_id}/close").status_code == 204
    assert _draft_status(draft_id) == "expired"
    assert approver.decide(approval_id, "approve", csrf=approver.page_csrf()).status_code == 409
    assert service.post(f"/v1/inbox/drafts/{draft_id}/release").status_code == 409


def test_a_draft_with_no_approval_cannot_be_closed(service: httpx.Client) -> None:
    draft_id = _sql("select id from inbox.drafts where approval_id is null limit 1")
    if not draft_id:
        pytest.skip("every draft has an approval")
    status_before = _draft_status(draft_id)
    assert service.post(f"/v1/inbox/drafts/{draft_id}/close").status_code == 409
    assert _draft_status(draft_id) == status_before


@pytest.mark.parametrize("file", ["m15.eml", "m26.eml", "m27.eml"])
def test_held_injection_emails_get_no_draft(service: httpx.Client, file: str) -> None:
    message_id = _message_id(file)
    assert _sql(f"select quarantined from inbox.triage where message_id = '{message_id}'") == "t"
    run = service.post("/v1/runs", json={"workflow": "inbox", "n8n_execution_id": f"itest-{file}"})
    response = service.post(
        "/v1/inbox/drafts", json={"run_id": run.json()["run_id"], "message_id": message_id}
    )
    assert response.status_code == 409
    assert _sql(f"select count(*) from inbox.drafts where message_id = '{message_id}'") == "0"


def test_a_calmer_second_triage_never_releases_a_held_message() -> None:
    message_id = _message_id("m26.eml")
    _retriage(message_id, "clear")
    row = _sql(f"select quarantined, route from inbox.triage where message_id = '{message_id}'")
    assert row == "t|quarantine"


def test_a_hold_that_lands_after_drafting_still_blocks_sending(approver: ApproverClient) -> None:
    draft_id, approval_id, to = _take_pending_draft()
    message_id = _sql(f"select message_id from inbox.drafts where id = '{draft_id}'")
    before = _replies_to(to)
    _retriage(message_id, "held")
    assert approver.decide(approval_id, "approve", csrf=approver.page_csrf()).status_code == 303

    time.sleep(8)
    assert _draft_status(draft_id) == "pending"
    assert _replies_to(to) == before


def test_reply_to_is_flagged_and_never_used(approver: ApproverClient) -> None:
    message_id = _message_id("m28.eml")
    from_addr = _header("m28.eml", "From").split("<")[-1].rstrip(">").strip().lower()
    reply_to = _header("m28.eml", "Reply-To").split("<")[-1].rstrip(">").strip().lower()
    assert reply_to and reply_to != from_addr

    row = _sql(
        "select d.to_addr, d.reply_to_differs, d.approval_id from inbox.drafts d "
        f"where d.message_id = '{message_id}'"
    )
    if not row:
        pytest.skip("no draft was made for m28")
    to, differs, approval_id = row.split("|")
    assert to == from_addr
    assert differs == "t"
    if approval_id:
        page = approver.client.get(f"/approver/approvals/{approval_id}").text
        assert "Reply-To differs from From" in page
        assert f"<strong>{reply_to}" not in page.lower()


def test_a_lookalike_approval_is_not_shown_as_the_reply(
    approver: ApproverClient, service: httpx.Client
) -> None:
    draft_id = _sql("select id from inbox.drafts where approval_id is not null limit 1")
    if not draft_id:
        pytest.skip("no draft approval exists")
    n8n = f"http://localhost:{N8N_PORT}"
    run = service.post(
        "/v1/runs", json={"workflow": "inbox", "n8n_execution_id": "itest-lookalike"}
    )
    created = service.post(
        "/v1/approvals",
        json={
            "run_id": run.json()["run_id"],
            "kind": "inbox.send_reply",
            "subject": {"draft_id": draft_id, "to": "someone@else.example", "body": "Pay here."},
            "resume_url": f"{n8n}/webhook-waiting/123456789?signature={'ab' * 32}",
            "expires_in_s": 600,
        },
    )
    assert created.status_code == 201, created.text
    page = approver.client.get(f"/approver/approvals/{created.json()['approval_id']}").text
    assert "Approve and send" not in page


def test_service_token_cannot_open_a_draft_approval(api_url: str, service_token: str) -> None:
    approval_id = _sql("select approval_id from inbox.drafts where approval_id is not null limit 1")
    if not approval_id:
        pytest.skip("no draft approval exists")
    response = httpx.get(
        f"{api_url}/approver/approvals/{approval_id}",
        headers={"Authorization": f"Bearer {service_token}"},
    )
    assert response.status_code == 401
