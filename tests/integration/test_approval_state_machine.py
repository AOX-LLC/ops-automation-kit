"""Approvals move pending -> approved | rejected | expired exactly once."""

from __future__ import annotations

import concurrent.futures
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tests.integration.conftest import ApproverClient, audit_count, psql

pytestmark = pytest.mark.integration

MakeApproval = Callable[..., dict[str, Any]]


def status_of(service: httpx.Client, approval_id: str) -> str:
    response = service.get(f"/v1/approvals/{approval_id}")
    assert response.status_code == 200
    return str(response.json()["status"])


# agent-core decisions are "approve" / "reject"; the statuses they lead to stay
# "approved" / "rejected".
@pytest.mark.parametrize(("decision", "status"), [("approve", "approved"), ("reject", "rejected")])
def test_pending_to_decided(
    service: httpx.Client,
    approver: ApproverClient,
    make_approval: MakeApproval,
    decision: str,
    status: str,
) -> None:
    approval_id = make_approval()["approval_id"]
    assert status_of(service, approval_id) == "pending"

    response = approver.decide(approval_id, decision, csrf=approver.page_csrf())
    assert response.status_code == 303
    assert status_of(service, approval_id) == status
    assert audit_count("approval.decided", approval_id) == 1
    outbox = psql(f"select count(*) from core.outbox where approval_id = '{approval_id}'")
    assert outbox.stdout.strip() == "1", "a decision must queue exactly one n8n resume"


def test_second_decision_is_refused(
    service: httpx.Client, approver: ApproverClient, make_approval: MakeApproval
) -> None:
    approval_id = make_approval()["approval_id"]
    assert approver.decide(approval_id, "approve", csrf=approver.page_csrf()).status_code == 303

    again = approver.decide(approval_id, "reject", csrf=approver.page_csrf())
    assert again.status_code == 409
    assert status_of(service, approval_id) == "approved"
    assert audit_count("approval.decided", approval_id) == 1


def test_concurrent_decisions_have_one_winner(
    approver: ApproverClient, make_approval: MakeApproval
) -> None:
    approval_id = make_approval()["approval_id"]
    csrf = approver.page_csrf()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda decision: approver.decide(approval_id, decision, csrf=csrf).status_code,
                ["approve", "reject", "approve", "reject"],
            )
        )
    assert sorted(results) == [303, 409, 409, 409]
    assert audit_count("approval.decided", approval_id) == 1


def test_expiry_by_time(
    service: httpx.Client, approver: ApproverClient, make_approval: MakeApproval
) -> None:
    approval_id = make_approval(expires_in_s=1)["approval_id"]
    time.sleep(2)
    response = approver.decide(approval_id, "approve", csrf=approver.page_csrf())
    assert response.status_code == 409
    assert "expired" in response.text
    assert status_of(service, approval_id) == "expired"
    assert audit_count("approval.decided", approval_id) == 0


def test_expire_call_then_decision_is_refused(
    service: httpx.Client, approver: ApproverClient, make_approval: MakeApproval
) -> None:
    approval_id = make_approval()["approval_id"]
    expired = service.post(f"/v1/approvals/{approval_id}/expire")
    assert expired.status_code == 200
    assert expired.json()["status"] == "expired"
    assert service.post(f"/v1/approvals/{approval_id}/expire").json()["status"] == "expired"
    response = approver.decide(approval_id, "approve", csrf=approver.page_csrf())
    assert response.status_code == 409
    assert status_of(service, approval_id) == "expired"


def test_expire_does_not_undo_a_decision(
    service: httpx.Client, approver: ApproverClient, make_approval: MakeApproval
) -> None:
    approval_id = make_approval()["approval_id"]
    approver.decide(approval_id, "reject", csrf=approver.page_csrf())
    assert service.post(f"/v1/approvals/{approval_id}/expire").json()["status"] == "rejected"


def test_resume_url_never_leaves_the_helper(
    service: httpx.Client, approver: ApproverClient, make_approval: MakeApproval
) -> None:
    approval = make_approval()
    approval_id = approval["approval_id"]
    stored = psql(
        f"select resume_url from core.approvals where id = '{approval_id}'"
    ).stdout.strip()
    signature = stored.split("signature=")[1]
    approver.decide(approval_id, "approve", csrf=approver.page_csrf())

    surfaces = [
        str(approval),
        service.get(f"/v1/approvals/{approval_id}").text,
        approver.client.get("/approver/").text,
        approver.client.get(f"/approver/approvals/{approval_id}").text,
        psql(f"select payload from core.audit_log where subject_id = '{approval_id}'").stdout,
    ]
    for surface in surfaces:
        assert signature not in surface
        assert "webhook-waiting" not in surface
