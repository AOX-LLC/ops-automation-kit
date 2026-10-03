"""A stored approval that cannot be parsed never crashes a reader or stops the expiry sweep.

The database bounds keep such rows out, so these tests plant them as a superuser with the guards
switched off for one transaction (the way a row stored before the bounds, or one a bound cannot
express, would look). They then go through the real readers: the approver page, the decision
route, the service API, the listing and the sweep. Planted rows are closed afterwards so none
stays live for another test.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import httpx
import pytest

from tests.integration.conftest import ApproverClient, audit_count, psql
from tests.integration.test_approval_roles import in_api

pytestmark = pytest.mark.integration

GUARDS = ("approvals_bounds", "approvals_guard")
ZERO_HASH = "repeat(md5(random()::text), 2)"


def _plant_sql(approval_id: str, **columns: str) -> str:
    row = {
        "id": f"'{approval_id}'",
        "action": "'kit_smoke.echo'",
        "summary": "'planted'",
        "payload": "'{}'::jsonb",
        "payload_sha256": ZERO_HASH,
        "requested_by": "'service.n8n'",
        "required_role": "'approver'",
        "created_at": "now()",
        "expires_at": "now() + interval '1 hour'",
        **columns,
    }
    off = "".join(f"alter table core.approvals disable trigger {name}; " for name in GUARDS)
    on = "".join(f"alter table core.approvals enable always trigger {name}; " for name in GUARDS)
    return (
        f"{off}insert into core.approvals ({', '.join(row)}) values ({', '.join(row.values())}); "
        f"{on}"
    )


@pytest.fixture
def plant() -> Iterator[Any]:
    planted: list[str] = []

    def make(**columns: str) -> str:
        approval_id = str(uuid4())
        result = psql(_plant_sql(approval_id, **columns))
        assert result.returncode == 0, result.stderr
        planted.append(approval_id)
        return approval_id

    yield make
    if planted:
        ids = ", ".join(f"'{approval_id}'" for approval_id in planted)
        off = "".join(f"alter table core.approvals disable trigger {name}; " for name in GUARDS)
        on = "".join(
            f"alter table core.approvals enable always trigger {name}; " for name in GUARDS
        )
        psql(
            f"{off}update core.approvals set status = 'cancelled', closed_at = now() "
            f"where id in ({ids}) and status = 'pending'; {on}"
        )


def stored(approval_id: str, columns: str = "status") -> str:
    return psql(f"select {columns} from core.approvals where id = '{approval_id}'").stdout.strip()


def due() -> dict[str, str]:
    return {
        "created_at": "now() - interval '3 hours'",
        "expires_at": "now() - interval '2 hours'",
    }


def test_the_sweep_closes_a_row_whose_run_context_cannot_be_parsed() -> None:
    """One poison row used to roll back its whole batch, every minute, for every request."""
    honest, poison = str(uuid4()), str(uuid4())
    for approval_id, context in (
        (honest, "null"),
        (poison, """'{"run_id": "r1", "external_ids": {"Not A Name": "x"}}'::jsonb"""),
    ):
        result = psql(_plant_sql(approval_id, run_context=context, **due()))
        assert result.returncode == 0, result.stderr
    swept = in_api(
        """
        async def main(queue):
            return {"swept": await queue.expire_due(principal=SWEEP)}
        """
    )
    assert isinstance(swept["swept"], int)
    # Whoever swept (the api's own 60 s sweep may have), both rows ended up expired and audited.
    for approval_id in (honest, poison):
        assert stored(approval_id, "status, closed_at = expires_at") == "expired|t"
        assert audit_count("approval.expired", approval_id) == 1
    note = psql(
        "select payload from core.audit_log where action = 'approval.expired' "
        f"and subject_id = '{poison}'"
    ).stdout
    assert '"run_context":"unreadable"' in note.replace(" ", "")


def test_a_secret_shaped_context_name_gets_through_the_bounds_and_the_sweep_copes() -> None:
    """The database cannot tell api_key from a real name (it matches the pattern); the reader
    must cope with a context agent-core refuses."""
    approval_id = str(uuid4())
    # The requester role's own insert path: a one-second lifetime, then let it lapse.
    inserted = psql(
        "insert into core.approvals (id, action, summary, payload, payload_sha256, requested_by, "
        "required_role, created_at, expires_at, run_context) values "
        f"('{approval_id}', 'kit_smoke.echo', 's', '{{}}', repeat(md5(random()::text), 2), "
        "'service.n8n', "
        "'approver', now(), now() + interval '1 second', "
        """'{"run_id": "r1", "external_ids": {"api_key": "abc"}}')""",
        role="opskit_app",
    )
    assert inserted.returncode == 0, inserted.stderr
    time.sleep(2.5)
    in_api(
        """
        async def main(queue):
            return {"swept": await queue.expire_due(principal=SWEEP)}
        """
    )
    assert stored(approval_id, "status") == "expired"
    assert audit_count("approval.expired", approval_id) == 1


def test_an_unparsable_row_does_not_stop_the_sweep_for_the_others(plant: Any) -> None:
    poison = [
        plant(run_context="""'{"run_id": "r1", "external_ids": {"Bad": "x"}}'::jsonb""", **due())
        for _ in range(3)
    ]
    honest = plant(**due())
    in_api(
        """
        async def main(queue):
            return {"swept": await queue.expire_due(principal=SWEEP, limit=2)}
        """
    )
    for approval_id in [*poison, honest]:
        assert stored(approval_id) == "expired"


def test_a_payload_that_is_not_an_object_is_refused_not_crashed(
    plant: Any, approver: ApproverClient
) -> None:
    approval_id = plant(payload="'[1]'::jsonb")
    csrf = approver.page_csrf()
    for _ in range(2):  # twice: the audit note must be written once
        assert approver.client.get(f"/approver/approvals/{approval_id}").status_code == 409
        assert approver.decide(approval_id, "approve", csrf=csrf).status_code == 409
    assert stored(approval_id) == "pending"
    outbox = psql(f"select count(*) from core.outbox where approval_id = '{approval_id}'")
    assert outbox.stdout.strip() == "0"
    assert audit_count("approval.unreadable", approval_id) == 1
    assert audit_count("approval.decided", approval_id) == 0


def test_a_row_the_model_refuses_is_skipped_by_the_listing_and_refused_by_reads(
    plant: Any, approver: ApproverClient, service: httpx.Client
) -> None:
    broken = plant(summary="repeat('s', 600)")
    listing = approver.client.get("/approver/")
    assert listing.status_code == 200
    assert broken not in listing.text
    assert service.get(f"/v1/approvals/{broken}").status_code == 409
    assert approver.client.get(f"/approver/approvals/{broken}").status_code == 409
    csrf = approver.page_csrf()
    assert approver.decide(broken, "approve", csrf=csrf).status_code == 409
    assert stored(broken) == "pending"
    assert audit_count("approval.unreadable", broken) == 1


def _every_listed_page(approver: ApproverClient) -> str:
    """The text of every page of the pending listing, following the cursor to the end."""
    pages, url = [], "/approver/"
    for _ in range(200):
        response = approver.client.get(url)
        assert response.status_code == 200
        pages.append(response.text)
        match = re.search(r'href="(/approver/\?cursor=[^"]+)"', response.text)
        if match is None:
            break
        url = match.group(1).replace("&amp;", "&")
    return "".join(pages)


def test_a_listing_still_pages_past_an_unreadable_row(
    plant: Any, approver: ApproverClient, make_approval: Any
) -> None:
    broken = plant(summary="repeat('s', 600)", created_at="now() - interval '10 minutes'")
    fine = make_approval()["approval_id"]
    listing = _every_listed_page(approver)
    # Earlier tests leave pending approvals, so the new one may be on a later page; paging
    # counts the rows fetched, so the unreadable one in front of it hides nothing.
    assert fine in listing
    assert broken not in listing


def test_an_ordinary_approval_is_not_reported_unreadable(
    approver: ApproverClient, make_approval: Any
) -> None:
    approval_id = make_approval()["approval_id"]
    assert approver.client.get(f"/approver/approvals/{approval_id}").status_code == 200
    assert audit_count("approval.unreadable", approval_id) == 0
