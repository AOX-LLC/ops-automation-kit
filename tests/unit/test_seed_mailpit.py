from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from opskit.config import Settings
from opskit.seed.mailpit import load_emails, read_headers, search_query

RAW = (
    b"From: a@x.example\r\nTo: b@y.example\r\n"
    b"Message-ID: <abc.123@x.example>\r\nSubject: Hi\r\n\r\nBody\r\n"
)


def test_read_headers_strips_angle_brackets() -> None:
    assert read_headers(RAW) == ("abc.123@x.example", "a@x.example", "b@y.example")


def test_read_headers_requires_message_id() -> None:
    with pytest.raises(ValueError, match="Message-ID"):
        read_headers(b"From: a@x.example\r\nTo: b@y.example\r\n\r\n")


def test_search_query_is_quoted_and_escaped() -> None:
    assert search_query("abc@x.example") == 'message-id:"abc@x.example"'
    assert search_query('a"b') == 'message-id:"a\\"b"'


def test_load_emails_sends_only_absent_messages(tmp_path: Path) -> None:
    (tmp_path / "1.eml").write_bytes(RAW)
    (tmp_path / "2.eml").write_bytes(RAW.replace(b"abc.123", b"new.456"))
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        queries.append(query)
        present = "abc.123" in query
        return httpx.Response(200, json={"messages_count": 1 if present else 0, "messages": []})

    sent: list[tuple[str, str, bytes]] = []

    def fake_send(_settings: Settings, sender: str, recipient: str, raw: bytes) -> None:
        sent.append((sender, recipient, raw))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    counts = load_emails(Settings(), tmp_path, client=client, send=fake_send)

    assert counts == (1, 1)
    assert queries == ['message-id:"abc.123@x.example"', 'message-id:"new.456@x.example"']
    assert sent == [("a@x.example", "b@y.example", RAW.replace(b"abc.123", b"new.456"))]
