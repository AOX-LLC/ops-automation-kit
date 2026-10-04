"""Fixtures for tests that run against the live compose stack."""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
API_PORT = os.environ.get("KIT_API_PORT", "4301")
N8N_PORT = os.environ.get("KIT_N8N_PORT", "4300")
API_URL = f"http://127.0.0.1:{API_PORT}"
SECRETS_DIR = "/run/kit-secrets"
# Roles whose password is not on the postgres container's secrets mount: read from the api's.
ROLE_PASSWORDS_FROM_API = {"opskit_approver": "opskit_approver_password"}
ROLE_PASSWORD_FILES = {
    "opskit_app": "opskit_app_password",
    "opskit_owner": "opskit_owner_password",
    "n8n_user": "n8n_db_password",
}
_CSRF_PATTERN = re.compile(r'name="csrf_token" value="([^"]*)"')


def compose(
    *args: str, check: bool = True, input: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=check,
        input=input,
    )


def _readyz_status() -> int | None:
    try:
        return httpx.get(f"{API_URL}/readyz", timeout=3).status_code
    except httpx.HTTPError:
        return None


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    here = Path(__file__).parent
    for item in items:
        if here in Path(str(item.path)).parents:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session", autouse=True)
def _stack_is_up() -> None:
    """These tests write audit rows and restart the api, so they only run when asked to.

    With KIT_INTEGRATION=1 (set by `make test` and CI) an unready stack is a failure, never a
    skip: a broken /readyz must not turn the whole suite green.
    """
    if os.environ.get("KIT_INTEGRATION") != "1":
        pytest.skip("integration tests are opt-in: run them with KIT_INTEGRATION=1 (make test)")
    if _readyz_status() != 200:
        pytest.fail(f"compose stack not ready: GET {API_URL}/readyz is not 200", pytrace=False)


@pytest.fixture(scope="session")
def api_url() -> str:
    return API_URL


@pytest.fixture(scope="session")
def service_token() -> str:
    result = compose("exec", "-T", "api", "cat", f"{SECRETS_DIR}/api_service_token")
    return result.stdout.strip()


@pytest.fixture(scope="session")
def approver_password() -> str:
    result = compose(
        "run", "--rm", "-T", "kit-login", "python", "-m", "opskit.bootstrap.show_login", "--env"
    )
    for line in result.stdout.splitlines():
        if line.startswith("KIT_APPROVER_PASSWORD="):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise RuntimeError("KIT_APPROVER_PASSWORD not found in show_login output")


@pytest.fixture
def service(api_url: str, service_token: str) -> Iterator[httpx.Client]:
    with httpx.Client(
        base_url=api_url, headers={"Authorization": f"Bearer {service_token}"}, timeout=15
    ) as client:
        yield client


class ApproverClient:
    """Browser-like client for the approver page, with its own cookie jar."""

    def __init__(self, api_url: str) -> None:
        self.client = httpx.Client(base_url=api_url, follow_redirects=False, timeout=15)

    @staticmethod
    def csrf_from(html: str) -> str:
        match = _CSRF_PATTERN.search(html)
        if match is None:
            raise ValueError("no csrf_token field in page")
        return match.group(1)

    def open_login(self) -> str:
        return self.csrf_from(self.client.get("/approver/login").text)

    def login(self, password: str) -> httpx.Response:
        csrf = self.open_login()
        return self.client.post("/approver/login", data={"password": password, "csrf_token": csrf})

    def page_csrf(self) -> str:
        return self.csrf_from(self.client.get("/approver/").text)

    def decide(
        self, approval_id: str, decision: str, *, csrf: str | None, note: str = ""
    ) -> httpx.Response:
        data = {"decision": decision, "note": note}
        if csrf is not None:
            data["csrf_token"] = csrf
        return self.client.post(f"/approver/approvals/{approval_id}/decision", data=data)

    def logout(self, csrf: str | None) -> httpx.Response:
        data = {} if csrf is None else {"csrf_token": csrf}
        return self.client.post("/approver/logout", data=data)

    @property
    def cookies(self) -> httpx.Cookies:
        return self.client.cookies

    def close(self) -> None:
        self.client.close()


@pytest.fixture
def new_approver_client(api_url: str) -> Iterator[Callable[[], ApproverClient]]:
    created: list[ApproverClient] = []

    def factory() -> ApproverClient:
        client = ApproverClient(api_url)
        created.append(client)
        return client

    yield factory
    for client in created:
        client.close()


@pytest.fixture
def approver(
    new_approver_client: Callable[[], ApproverClient], approver_password: str
) -> ApproverClient:
    client = new_approver_client()
    response = client.login(approver_password)
    assert response.status_code == 303
    return client


def new_approval(
    service: httpx.Client, *, expires_in_s: int = 600, kind: str = "kit_smoke.echo"
) -> dict[str, Any]:
    run = service.post(
        "/v1/runs",
        json={"workflow": "kit_smoke", "n8n_execution_id": f"itest-{uuid4()}"},
    )
    assert run.status_code == 201, run.text
    waiting_id = secrets.randbelow(900_000_000) + 100_000_000
    resume_url = (
        f"http://localhost:{N8N_PORT}/webhook-waiting/{waiting_id}"
        f"?signature={secrets.token_hex(32)}"
    )
    created = service.post(
        "/v1/approvals",
        json={
            "run_id": run.json()["run_id"],
            "kind": kind,
            "subject": {"test": True, "nonce": str(uuid4())},
            "resume_url": resume_url,
            "expires_in_s": expires_in_s,
        },
    )
    assert created.status_code == 201, created.text
    body: dict[str, Any] = created.json()
    return body


@pytest.fixture
def make_approval(service: httpx.Client) -> Callable[..., dict[str, Any]]:
    def make(**kwargs: Any) -> dict[str, Any]:
        return new_approval(service, **kwargs)

    return make


def psql(
    sql: str, *, role: str = "postgres", db: str = "opskit"
) -> subprocess.CompletedProcess[str]:
    """Run one statement inside the postgres container; never raises on failure."""
    flags = ["-v", "ON_ERROR_STOP=1", "-tA"]
    if role in ROLE_PASSWORDS_FROM_API:
        password = compose(
            "exec", "-T", "api", "cat", f"{SECRETS_DIR}/{ROLE_PASSWORDS_FROM_API[role]}"
        ).stdout.strip()
        args = ["psql", "-h", "127.0.0.1", "-U", role, "-d", db, *flags, "-c", sql]
        return compose("exec", "-T", "-e", f"PGPASSWORD={password}", "postgres", *args, check=False)
    if role == "postgres":
        args = ["psql", "-U", "postgres", "-d", db, *flags, "-c", sql]
    else:
        secret = ROLE_PASSWORD_FILES[role]
        script = (
            f'PGPASSWORD="$(cat {SECRETS_DIR}/{secret})" '
            f'psql -h 127.0.0.1 -U {role} -d {db} -v ON_ERROR_STOP=1 -tA -c "$1"'
        )
        args = ["sh", "-c", script, "_", sql]
    return compose("exec", "-T", "postgres", *args, check=False)


@pytest.fixture
def finish_test_runs() -> Iterator[None]:
    """Close the runs a test opened. A run of receipts, leads or inbox is refused while another is
    running, and these tests start runs as fixtures without ever finishing them. n8n's own
    execution ids are numbers; every test id is prefixed."""
    yield
    result = psql(
        "update core.runs set status = 'failed', finished_at = now() "
        "where status = 'running' and n8n_execution_id !~ '^[0-9]+$'"
    )
    assert result.returncode == 0, result.stderr


def age_approval(approval_id: str) -> None:
    """Make a pending approval's lifetime lapse. The guard trigger fixes `expires_at`, so a test
    that needs time to pass switches it off for this one statement, as a superuser, and back
    on (always-on, as the migration leaves it)."""
    result = psql(
        "alter table core.approvals disable trigger approvals_guard; "
        "update core.approvals set expires_at = now() - interval '1 second', "
        "created_at = now() - interval '2 seconds' "
        f"where id = '{_quote(approval_id)}'; "
        "alter table core.approvals enable always trigger approvals_guard"
    )
    assert result.returncode == 0, result.stderr


def audit_count(action: str, subject_id: str | None = None) -> int:
    sql = f"select count(*) from core.audit_log where action = '{_quote(action)}'"
    if subject_id is not None:
        sql += f" and subject_id = '{_quote(subject_id)}'"
    result = psql(sql)
    assert result.returncode == 0, result.stderr
    return int(result.stdout.strip())


def _quote(value: str) -> str:
    return value.replace("'", "''")


def restart_api() -> None:
    compose("restart", "api")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if _readyz_status() == 200:
            return
        time.sleep(1)
    raise RuntimeError("api did not become ready within 60 s of restart")
