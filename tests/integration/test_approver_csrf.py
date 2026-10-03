"""Every state-changing approver POST requires the session's own CSRF token."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tests.integration.conftest import ApproverClient, audit_count

pytestmark = pytest.mark.integration

MakeApproval = Callable[..., dict[str, Any]]


def pending(service: httpx.Client, approval_id: str) -> bool:
    return bool(service.get(f"/v1/approvals/{approval_id}").json()["status"] == "pending")


@pytest.mark.parametrize("csrf", [None, "", "not-the-token"])
def test_decision_without_the_right_token_is_refused(
    service: httpx.Client,
    approver: ApproverClient,
    make_approval: MakeApproval,
    csrf: str | None,
) -> None:
    approval_id = make_approval()["approval_id"]
    response = approver.decide(approval_id, "approve", csrf=csrf)
    assert response.status_code == 403
    assert pending(service, approval_id)
    assert audit_count("approval.decided", approval_id) == 0


def test_another_sessions_token_is_refused(
    service: httpx.Client,
    approver: ApproverClient,
    new_approver_client: Callable[[], ApproverClient],
    approver_password: str,
    make_approval: MakeApproval,
) -> None:
    other = new_approver_client()
    assert other.login(approver_password).status_code == 303
    foreign_token = other.page_csrf()

    approval_id = make_approval()["approval_id"]
    assert approver.decide(approval_id, "approve", csrf=foreign_token).status_code == 403
    assert pending(service, approval_id)


def test_login_requires_the_form_token(
    new_approver_client: Callable[[], ApproverClient], approver_password: str
) -> None:
    client = new_approver_client()
    client.open_login()
    response = client.client.post("/approver/login", data={"password": approver_password})
    assert response.status_code == 403
    assert "kit_approver" not in response.headers.get("set-cookie", "")


def test_logout_requires_the_token(approver: ApproverClient) -> None:
    assert approver.logout(csrf=None).status_code == 403
    assert approver.client.get("/approver/").status_code == 200, "session should survive"
