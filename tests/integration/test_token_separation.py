"""n8n's service token can request approvals but can never reach the approver page or decide."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tests.integration.conftest import ApproverClient, psql

pytestmark = pytest.mark.integration

MakeApproval = Callable[..., dict[str, Any]]


@pytest.mark.parametrize("path", ["/approver/", "/approver/login"])
def test_service_token_cannot_open_approver_pages(
    api_url: str, service_token: str, path: str
) -> None:
    response = httpx.get(f"{api_url}{path}", headers={"Authorization": f"Bearer {service_token}"})
    assert response.status_code == 401


def test_service_token_cannot_decide_even_alongside_a_valid_session(
    service: httpx.Client,
    approver: ApproverClient,
    service_token: str,
    make_approval: MakeApproval,
) -> None:
    approval_id = make_approval()["approval_id"]
    response = approver.client.post(
        f"/approver/approvals/{approval_id}/decision",
        data={"decision": "approve", "csrf_token": approver.page_csrf()},
        headers={"Authorization": f"Bearer {service_token}"},
    )
    assert response.status_code == 401
    assert service.get(f"/v1/approvals/{approval_id}").json()["status"] == "pending"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/approvals/{id}/decision"),
        ("POST", "/v1/approvals/{id}/approve"),
        ("PATCH", "/v1/approvals/{id}"),
        ("PUT", "/v1/approvals/{id}"),
        ("POST", "/v1/approvals/{id}"),
    ],
)
def test_no_service_route_decides(
    service: httpx.Client, make_approval: MakeApproval, method: str, path: str
) -> None:
    approval_id = make_approval()["approval_id"]
    body = {"decision": "approve", "status": "approved"}
    response = service.request(method, path.format(id=approval_id), json=body)
    assert response.status_code in (404, 405)
    assert service.get(f"/v1/approvals/{approval_id}").json()["status"] == "pending"


def test_approver_cookie_does_not_authenticate_service_routes(
    approver: ApproverClient, make_approval: MakeApproval
) -> None:
    approval_id = make_approval()["approval_id"]
    cookies = {name: value for name, value in approver.client.cookies.items()}
    for request in (
        lambda c: c.get(f"/v1/approvals/{approval_id}"),
        lambda c: c.post(f"/v1/approvals/{approval_id}/expire"),
    ):
        with httpx.Client(base_url=str(approver.client.base_url), cookies=cookies) as client:
            assert request(client).status_code == 401


def test_logout_revokes_the_session_server_side(
    new_approver_client: Callable[[], ApproverClient], approver_password: str
) -> None:
    client = new_approver_client()
    assert client.login(approver_password).status_code == 303
    stolen = {name: value for name, value in client.client.cookies.items()}
    assert client.logout(csrf=client.page_csrf()).status_code == 303

    with httpx.Client(
        base_url=str(client.client.base_url), cookies=stolen, follow_redirects=False
    ) as replay:
        response = replay.get("/approver/")
        assert response.status_code == 303
        assert response.headers["location"] == "/approver/login"


def test_session_past_its_absolute_expiry_is_refused(
    new_approver_client: Callable[[], ApproverClient], approver_password: str
) -> None:
    client = new_approver_client()
    assert client.login(approver_password).status_code == 303
    assert client.client.get("/approver/").status_code == 200
    # Age this client's session (the newest row) past its absolute expiry.
    aged = psql(
        "update core.approver_sessions set expires_at = now() - interval '1 second' "
        "where id = (select id from core.approver_sessions order by created_at desc limit 1)"
    )
    assert aged.returncode == 0, aged.stderr
    response = client.client.get("/approver/")
    assert response.status_code == 303
    assert response.headers["location"] == "/approver/login"


def test_login_form_token_dies_at_login(
    new_approver_client: Callable[[], ApproverClient],
    approver_password: str,
    make_approval: MakeApproval,
) -> None:
    client = new_approver_client()
    pre_login_token = client.open_login()
    assert (
        client.client.post(
            "/approver/login", data={"password": approver_password, "csrf_token": pre_login_token}
        ).status_code
        == 303
    )
    approval_id = make_approval()["approval_id"]
    assert client.decide(approval_id, "approve", csrf=pre_login_token).status_code == 403
