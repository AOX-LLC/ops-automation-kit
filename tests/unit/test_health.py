"""/healthz reports build identity and degrades to schema_version null when the DB is down."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opskit.api.routers import health


class _BrokenEngine:
    def connect(self) -> Any:
        raise ConnectionRefusedError("database is down")


def _client(monkeypatch: pytest.MonkeyPatch, **env: str) -> TestClient:
    for name in ("OPSKIT_BUILD_COMMIT", "OPSKIT_BUILD_BRANCH"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    app = FastAPI()
    app.state.engine = _BrokenEngine()
    app.state.build_info = health.load_build_info()
    app.include_router(health.router)
    return TestClient(app)


def test_reports_build_env_and_survives_database_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, OPSKIT_BUILD_COMMIT="abc123", OPSKIT_BUILD_BRANCH="main")
    response = client.get("/healthz")
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["commit"] == "abc123"
    assert body["branch"] == "main"
    assert body["commit_source"] == "process_start"
    assert body["schema_version"] is None
    assert isinstance(body["uptime_s"], int)
    assert isinstance(body["version"], str)


def test_unset_or_empty_env_is_null_never_fabricated(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _client(monkeypatch, OPSKIT_BUILD_COMMIT="").get("/healthz").json()
    assert body["commit"] is None
    assert body["branch"] is None
