"""A second trigger of a workflow (the schedule beside a webhook call) starts no second run."""

from __future__ import annotations

import time
from collections.abc import Iterator
from uuid import uuid4

import httpx
import pytest

from tests.integration.conftest import N8N_PORT, psql

pytestmark = pytest.mark.integration

WORKFLOW = "leads"


def _start(service: httpx.Client, execution_id: str) -> httpx.Response:
    return service.post("/v1/runs", json={"workflow": WORKFLOW, "n8n_execution_id": execution_id})


@pytest.fixture
def started(service: httpx.Client) -> Iterator[list[str]]:
    """Run ids to finish afterwards, so a test never leaves a workflow blocked for the next."""
    run_ids: list[str] = []
    yield run_ids
    for run_id in run_ids:
        service.post(f"/v1/runs/{run_id}/finish", json={"status": "failed"})


def test_a_second_start_is_refused_while_the_first_runs(
    service: httpx.Client, started: list[str]
) -> None:
    first = _start(service, f"it-{uuid4().hex[:12]}")
    assert first.status_code == 201
    started.append(first.json()["run_id"])

    second = _start(service, f"it-{uuid4().hex[:12]}")
    assert second.status_code == 409
    assert first.json()["run_id"] in second.json()["detail"]


def test_a_retry_of_the_same_execution_is_not_an_overlap(
    service: httpx.Client, started: list[str]
) -> None:
    execution_id = f"it-{uuid4().hex[:12]}"
    first = _start(service, execution_id)
    started.append(first.json()["run_id"])
    retry = _start(service, execution_id)
    assert retry.status_code == 201
    assert retry.json()["run_id"] == first.json()["run_id"]


def test_the_next_run_starts_once_the_first_finishes(
    service: httpx.Client, started: list[str]
) -> None:
    first = _start(service, f"it-{uuid4().hex[:12]}")
    service.post(f"/v1/runs/{first.json()['run_id']}/finish", json={"status": "succeeded"})
    second = _start(service, f"it-{uuid4().hex[:12]}")
    assert second.status_code == 201
    started.append(second.json()["run_id"])


def test_the_smoke_workflow_may_overlap_itself(service: httpx.Client, started: list[str]) -> None:
    """It waits for a human approval, so a second run while it waits is legitimate."""
    for _ in range(2):
        response = service.post(
            "/v1/runs", json={"workflow": "kit_smoke", "n8n_execution_id": f"it-{uuid4().hex[:12]}"}
        )
        assert response.status_code == 201
        started.append(response.json()["run_id"])


def test_two_simultaneous_starts_let_exactly_one_through(
    service: httpx.Client, started: list[str]
) -> None:
    """The advisory lock is what stops the schedule and a webhook call both starting a run."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: _start(service, f"it-{uuid4().hex[:12]}"), range(2)))
    codes = sorted(response.status_code for response in responses)
    assert codes == [201, 409]
    started.extend(r.json()["run_id"] for r in responses if r.status_code == 201)


def test_a_start_with_no_execution_id_is_still_refused_while_one_runs(
    service: httpx.Client, started: list[str]
) -> None:
    first = service.post("/v1/runs", json={"workflow": WORKFLOW})
    assert first.status_code == 201
    started.append(first.json()["run_id"])
    assert service.post("/v1/runs", json={"workflow": WORKFLOW}).status_code == 409


def _fail_by_execution(
    service: httpx.Client, execution_id: str, status: str = "failed"
) -> httpx.Response:
    return service.post(f"/v1/runs/by-execution/{execution_id}/finish", json={"status": status})


def test_the_error_workflows_call_closes_the_failed_run_and_frees_the_workflow(
    service: httpx.Client, started: list[str]
) -> None:
    execution_id = f"it-{uuid4().hex[:12]}"
    first = _start(service, execution_id)
    started.append(first.json()["run_id"])
    assert _start(service, f"it-{uuid4().hex[:12]}").status_code == 409

    closed = _fail_by_execution(service, execution_id)
    assert closed.status_code == 200
    assert closed.json() == {"closed": True}

    second = _start(service, f"it-{uuid4().hex[:12]}")
    assert second.status_code == 201
    started.append(second.json()["run_id"])


def test_closing_an_unknown_or_finished_run_changes_nothing(
    service: httpx.Client, started: list[str]
) -> None:
    assert _fail_by_execution(service, f"it-{uuid4().hex[:12]}").json() == {"closed": False}
    execution_id = f"it-{uuid4().hex[:12]}"
    run = _start(service, execution_id).json()["run_id"]
    service.post(f"/v1/runs/{run}/finish", json={"status": "succeeded"})
    assert _fail_by_execution(service, execution_id).json() == {"closed": False}
    status = psql(f"select status from core.runs where id = '{run}'").stdout.strip()
    assert status == "succeeded"


def test_the_close_call_can_only_mark_a_run_failed(service: httpx.Client) -> None:
    assert _fail_by_execution(service, "it-anything", status="succeeded").status_code == 422


def test_a_failed_execution_starts_the_error_workflow(
    service: httpx.Client, started: list[str], webhook_token: str
) -> None:
    """The refused Start run fails the leads execution, so n8n must start the error workflow."""
    blocker = _start(service, f"it-{uuid4().hex[:12]}")
    started.append(blocker.json()["run_id"])
    count_sql = (
        "select count(*) from execution_entity "
        "where \"workflowId\" = 'runError00000001' and status = 'success'"
    )
    before = int(psql(count_sql, db="n8n").stdout.strip())

    called = httpx.post(
        f"http://127.0.0.1:{N8N_PORT}/webhook/leads-run",
        headers={"X-Kit-Token": webhook_token},
        timeout=15,
    )
    assert called.status_code == 200

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if int(psql(count_sql, db="n8n").stdout.strip()) > before:
            return
        time.sleep(2)
    pytest.fail("the error workflow did not run after the leads execution failed")
