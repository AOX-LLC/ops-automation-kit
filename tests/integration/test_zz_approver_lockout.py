"""Five wrong passwords lock the approver login, even against the right password.

Named to run last: it locks the shared approver account, and restarts the api afterwards
to clear the in-memory lock for whatever runs next.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from tests.integration.conftest import ApproverClient, psql, restart_api

pytestmark = pytest.mark.integration


@pytest.fixture
def clears_lock_afterwards() -> Iterator[None]:
    yield
    restart_api()


def failures() -> int:
    sql = (
        "select count(*) from core.audit_log "
        "where action = 'approver.login' and payload::jsonb->>'outcome' = 'failure'"
    )
    return int(psql(sql).stdout.strip())


def test_lockout_after_five_failures(
    new_approver_client: Callable[[], ApproverClient],
    approver_password: str,
    clears_lock_afterwards: None,
) -> None:
    restart_api()  # start from a clean throttle regardless of earlier runs
    client = new_approver_client()
    before = failures()
    for _ in range(5):
        assert client.login("definitely-not-the-password").status_code == 401
    assert failures() == before + 5

    locked = client.login(approver_password)
    assert locked.status_code == 429, "the right password must not get through a lockout"
    assert client.client.get("/approver/").status_code == 303, "no session may come out of it"
