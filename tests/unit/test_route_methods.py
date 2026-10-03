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
