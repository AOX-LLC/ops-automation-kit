"""A route that writes rows must not be a GET: a crawler, prefetch or retry could trigger it."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.routing import APIRoute

from opskit.api.routers import inbox, leads


def methods(router: APIRouter, path: str) -> set[str]:
    routes = [r for r in router.routes if isinstance(r, APIRoute) and r.path == path]
    assert len(routes) == 1, path
    return set(routes[0].methods or ())


def test_listing_new_mail_stores_it_so_it_is_a_post() -> None:
    assert methods(inbox.router, "/v1/inbox/pending") == {"POST"}


def test_researching_a_company_is_a_post_and_the_summary_only_reads() -> None:
    assert methods(leads.router, "/v1/leads/research") == {"POST"}
    assert methods(leads.router, "/v1/leads/summary") == {"GET"}


def test_the_leads_routes_need_the_service_token() -> None:
    from uuid import uuid4

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.state.service_token = "t" * 32
    app.include_router(leads.router)
    client = TestClient(app)
    body = {"run_id": str(uuid4()), "company_name": "Acme", "city_hint": "X"}
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        assert client.post("/v1/leads/research", json=body, headers=headers).status_code == 401
        summary = client.get(
            "/v1/leads/summary", params={"run_id": body["run_id"]}, headers=headers
        )
        assert summary.status_code == 401


def test_a_blank_company_name_is_refused() -> None:
    from uuid import uuid4

    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        leads.ResearchRequest(run_id=uuid4(), company_name="   ")
