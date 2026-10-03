from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opskit.api.routers import inputs
from opskit.config import Settings

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _touch(path: Path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_list_receipt_files_filters_extensions_and_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "inbox"
    _touch(root / "a.png")
    _touch(root / "b.PDF")
    _touch(root / "notes.txt")
    _touch(root / "sub" / "c.png")
    outside = tmp_path / "secret.png"
    _touch(outside)
    (root / "link.png").symlink_to(outside)
    (root / "escape.png").symlink_to(Path("..") / "secret.png")

    names = sorted(f.name for f in inputs.list_receipt_files(root, "samples", "receipts/inbox"))

    assert names == ["a.png", "b.PDF"]


def test_list_receipt_files_missing_root_is_empty(tmp_path: Path) -> None:
    assert inputs.list_receipt_files(tmp_path / "nope", "dropbox", "dropbox/receipts") == []


def test_page_after_key_walks_every_item_once() -> None:
    items = ["a", "b", "c", "d"]
    seen: list[str] = []
    cursor: str | None = None
    while True:
        page, next_cursor = inputs.page_after_key(items, str, inputs.decode_cursor(cursor), 1)
        seen += page
        if next_cursor is None:
            break
        cursor = next_cursor
    assert seen == items


def test_cursor_round_trip_and_garbage() -> None:
    assert inputs.decode_cursor(inputs.encode_cursor("samples/a.png")) == "samples/a.png"
    with pytest.raises(inputs.HTTPException):
        inputs.decode_cursor("!!!not base64")
    with pytest.raises(inputs.HTTPException):
        inputs.decode_offset(inputs.encode_cursor("abc"))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    samples = tmp_path / "samples"
    for name in ("r1.png", "r2.pdf", "r3.jpg"):
        _touch(samples / "receipts" / "inbox" / name, name.encode())
    _touch(tmp_path / "dropbox" / "receipts" / "d1.png", b"d")
    (samples / "leads").mkdir(parents=True)
    (samples / "leads" / "companies.csv").write_text(
        "company_name,city_hint\nA,X\nB,Y\nC,Z\n", encoding="utf-8"
    )
    return Settings(
        samples_dir=samples,
        dropbox_dir=tmp_path / "dropbox",
        mailpit_api_url="http://mailpit.test",
    )


def _client(settings: Settings, mailpit: httpx.MockTransport | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(inputs.router)
    app.state.service_token = TOKEN
    app.state.settings = settings
    if mailpit is not None:
        app.state.http_client = httpx.AsyncClient(transport=mailpit)
    return TestClient(app)


def _walk(client: TestClient, path: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(50):
        params: dict[str, Any] = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        body = client.get(path, params=params, headers=AUTH).json()
        items += body["items"]
        cursor = body["next_cursor"]
        if cursor is None:
            return items
    raise AssertionError("pagination did not terminate")


def test_requires_service_token(settings: Settings) -> None:
    assert _client(settings).get("/v1/leads/pending").status_code == 401


def test_receipts_pending_walks_all_files_sorted(settings: Settings) -> None:
    items = _walk(_client(settings), "/v1/receipts/pending")

    assert [(i["source"], i["path"]) for i in items] == [
        ("dropbox", "dropbox/receipts/d1.png"),
        ("samples", "receipts/inbox/r1.png"),
        ("samples", "receipts/inbox/r2.pdf"),
        ("samples", "receipts/inbox/r3.jpg"),
    ]
    assert items[2]["media_type"] == "application/pdf"
    assert len(items[0]["sha256"]) == 64


def test_leads_pending_pagination(settings: Settings) -> None:
    client = _client(settings)
    assert [i["company_name"] for i in _walk(client, "/v1/leads/pending")] == ["A", "B", "C"]
    whole = client.get("/v1/leads/pending", headers=AUTH).json()
    assert whole["next_cursor"] is None
    assert whole["items"][0] == {"company_name": "A", "city_hint": "X"}


@pytest.mark.parametrize("path", ["/v1/receipts/pending", "/v1/leads/pending"])
def test_bad_cursor_and_limit(settings: Settings, path: str) -> None:
    client = _client(settings)
    assert client.get(path, params={"cursor": "!!!"}, headers=AUTH).status_code == 400
    assert client.get(path, params={"limit": 0}, headers=AUTH).status_code == 422
    assert client.get(path, params={"limit": 101}, headers=AUTH).status_code == 422


def _mailpit(total: int = 2, fail: bool = False) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            return httpx.Response(500)
        start = int(request.url.params["start"])
        limit = int(request.url.params["limit"])
        messages = [
            {
                "ID": f"id{n}",
                "MessageID": f"m{n}@x.example",
                "From": {"Name": "A", "Address": "a@x.example"},
                "Subject": f"s{n}",
                "Created": "2026-08-01T10:00:00Z",
            }
            for n in range(start, min(start + limit, total))
        ]
        return httpx.Response(
            200, json={"total": total, "messages_count": total, "messages": messages}
        )

    return httpx.MockTransport(handler)


def test_inbox_pending_maps_and_paginates(settings: Settings) -> None:
    items = _walk(_client(settings, _mailpit()), "/v1/inbox/pending")

    assert [i["mailpit_id"] for i in items] == ["id0", "id1"]
    assert items[0] == {
        "mailpit_id": "id0",
        "message_id": "m0@x.example",
        "from": "a@x.example",
        "subject": "s0",
        "received_at": "2026-08-01T10:00:00Z",
    }


def test_inbox_pending_mailpit_error_is_502(settings: Settings) -> None:
    response = _client(settings, _mailpit(fail=True)).get("/v1/inbox/pending", headers=AUTH)

    assert response.status_code == 502
    assert response.json() == {"detail": "mailpit unavailable"}


def test_receipt_hash_is_cached_until_the_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs._hash_cache.clear()
    calls: list[Path] = []
    real = inputs.sha256_of_file

    def counting(path: Path) -> str:
        calls.append(path)
        return real(path)

    monkeypatch.setattr(inputs, "sha256_of_file", counting)
    path = tmp_path / "r.png"
    path.write_bytes(b"one")
    first = inputs._file_sha256(path)
    assert inputs._file_sha256(path) == first
    assert len(calls) == 1
    path.write_bytes(b"three")
    assert inputs._file_sha256(path) != first
    assert len(calls) == 2
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    inputs._file_sha256(path)
    assert len(calls) == 3


def test_receipt_hash_cache_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs._hash_cache.clear()
    monkeypatch.setattr(inputs, "HASH_CACHE_MAX_ENTRIES", 2)
    for name in ("a", "b", "c"):
        path = tmp_path / f"{name}.png"
        path.write_bytes(name.encode())
        inputs._file_sha256(path)
    assert len(inputs._hash_cache) == 2
