"""Smoke checks that the integration fixtures work against the live stack."""

from __future__ import annotations

import httpx

from tests.integration.conftest import ApproverClient, psql


def test_readyz(api_url: str) -> None:
    assert httpx.get(f"{api_url}/readyz", timeout=5).status_code == 200


def test_service_token_authenticates(service: httpx.Client) -> None:
    assert service.get("/v1/leads/pending", params={"limit": 1}).status_code == 200


def test_approver_login(approver: ApproverClient) -> None:
    assert approver.page_csrf()


def test_psql_as_postgres() -> None:
    result = psql("select 1")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"
