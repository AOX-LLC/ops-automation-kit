"""Ingestion clamps hostile mail to the database bounds. No database, no stack."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from opskit.inbox import limits
from opskit.inbox.limits import clamp_message
from opskit.inbox.mail import InboundMessage


def _message(**overrides: Any) -> InboundMessage:
    fields: dict[str, Any] = {
        "message_id": "m1@x.example",
        "mailpit_id": "id1",
        "from_header": "Cass <cass@mail.example>",
        "reply_to_header": "Cass <cass@mail.example>",
        "to_addr": "office@shop.example",
        "subject": "Question about a visit",
        "received_at": datetime(2026, 10, 3, 9, 30, tzinfo=UTC),
        "body_text": "Hi, how much is a visit? Call me on 555-0188.",
    }
    return InboundMessage.model_construct(**(fields | overrides))


def _clamped(**overrides: Any) -> InboundMessage:
    result = clamp_message(_message(**overrides))
    assert result is not None
    return result


def test_ordinary_mail_passes_through_unchanged() -> None:
    msg = _message()
    assert clamp_message(msg) == msg
    result = _clamped()
    for field in InboundMessage.model_fields:
        assert getattr(result, field) == getattr(msg, field)


def test_optional_fields_stay_none() -> None:
    result = _clamped(reply_to_header=None, to_addr=None, received_at=None)
    assert (result.reply_to_header, result.to_addr, result.received_at) == (None, None, None)


@pytest.mark.parametrize(
    ("field", "limit"),
    [
        ("subject", limits.SUBJECT_MAX),
        ("from_header", limits.HEADER_MAX),
        ("reply_to_header", limits.ADDRESS_LIST_MAX),
        ("to_addr", limits.ADDRESS_LIST_MAX),
        ("body_text", limits.BODY_MAX),
    ],
)
def test_oversize_text_is_cut_to_exactly_the_limit(field: str, limit: int) -> None:
    assert len(getattr(_clamped(**{field: "a" * (limit + 500)}), field)) == limit


def test_text_at_the_limit_is_untouched() -> None:
    body = "b" * limits.BODY_MAX
    assert _clamped(body_text=body).body_text == body


def test_multibyte_characters_are_not_split() -> None:
    subject = "é" * (limits.SUBJECT_MAX + 1)
    body = "\U0001f600" * (limits.BODY_MAX + 1)
    result = _clamped(subject=subject, body_text=body)
    assert result.subject == "é" * limits.SUBJECT_MAX
    assert result.body_text == "\U0001f600" * limits.BODY_MAX
    result.body_text.encode("utf-8")


def test_empty_subject_and_body_are_kept() -> None:
    result = _clamped(subject="", body_text="")
    assert (result.subject, result.body_text) == ("", "")


@pytest.mark.parametrize("year", [2150, 1900, 1])
def test_received_at_outside_the_range_becomes_none(year: int) -> None:
    assert _clamped(received_at=datetime(year, 6, 1, tzinfo=UTC)).received_at is None


def test_naive_received_at_is_judged_as_utc_and_kept_as_given() -> None:
    naive = datetime(2026, 6, 1, 12, 0)
    assert _clamped(received_at=naive).received_at == naive
    assert _clamped(received_at=datetime(2150, 1, 1)).received_at is None


def test_received_at_range_edges_are_inclusive() -> None:
    assert _clamped(received_at=limits.RECEIVED_MIN).received_at == limits.RECEIVED_MIN
    assert _clamped(received_at=limits.RECEIVED_MAX).received_at == limits.RECEIVED_MAX


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("message_id", ""),
        ("message_id", "x" * (limits.MESSAGE_ID_MAX + 1)),
        ("mailpit_id", ""),
        ("mailpit_id", "y" * (limits.MAILPIT_ID_MAX + 1)),
    ],
)
def test_unstorable_identity_is_skipped_with_a_content_free_warning(
    field: str, value: str, caplog: pytest.LogCaptureFixture
) -> None:
    subject, body = "SECRET-SUBJECT-TEXT", "SECRET-BODY-TEXT"
    with caplog.at_level(logging.WARNING, logger=limits.log.name):
        result = clamp_message(_message(**{field: value}, subject=subject, body_text=body))
    assert result is None
    assert len(caplog.records) == 1
    text = caplog.text
    assert field in text
    assert str(len(value)) in text
    assert subject not in text
    assert body not in text
    assert "cass@mail.example" not in text
    assert value == "" or value not in text


def test_identity_at_the_limit_is_stored() -> None:
    result = _clamped(
        message_id="m" * limits.MESSAGE_ID_MAX, mailpit_id="i" * limits.MAILPIT_ID_MAX
    )
    assert len(result.message_id) == limits.MESSAGE_ID_MAX


def test_constants_match_the_migration() -> None:
    migration = pytest.importorskip("opskit.db.migrations.versions.inbox_0003_bounds")
    rules = migration.MESSAGES
    assert rules["message_id"]["max"] == limits.MESSAGE_ID_MAX
    assert rules["mailpit_id"]["max"] == limits.MAILPIT_ID_MAX
    assert rules["from_header"]["max"] == limits.HEADER_MAX
    assert rules["reply_to_header"]["max"] == limits.ADDRESS_LIST_MAX
    assert rules["to_addr"]["max"] == limits.ADDRESS_LIST_MAX
    assert rules["subject"]["max"] == limits.SUBJECT_MAX
    assert rules["body_text"]["max"] == limits.BODY_MAX
    received = rules["received_at"]
    assert datetime.fromisoformat(received["after"]) == limits.RECEIVED_MIN
    assert datetime.fromisoformat(received["before"]) == limits.RECEIVED_MAX


def test_listing_leaves_out_unstorable_ids_before_the_limit_applies() -> None:
    import asyncio

    import httpx

    from opskit.inbox.limits import MAILPIT_ID_MAX, MESSAGE_ID_MAX
    from opskit.inbox.mail import MailpitSource

    hostile = [
        {"ID": f"h{i}", "MessageID": "m" * (MESSAGE_ID_MAX + 1), "From": {"Address": "a@x.example"}}
        for i in range(5)
    ]
    long_mailpit_id = {"ID": "p" * (MAILPIT_ID_MAX + 1), "MessageID": "ok-1"}
    fine = [
        {"ID": f"f{i}", "MessageID": f"fine-{i}", "From": {"Address": "a@x.example"}}
        for i in range(2)
    ]
    body = {"messages": [*hostile, long_mailpit_id, *fine], "messages_count": 8}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    async def known(_: object) -> set[str]:
        return set()

    async def run() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            refs = await MailpitSource("http://mailpit.example", client).list_new(
                known=known, limit=2
            )
        return [ref.message_id for ref in refs]

    # Five unstorable messages would have filled a limit of 2 had they been offered.
    assert sorted(asyncio.run(run())) == ["fine-0", "fine-1"]


def test_nul_and_lone_surrogates_are_cleaned_from_every_text_field() -> None:
    clamped = _clamped(
        subject="hi\x00 there\ud800!",
        body_text="a\x00b\udfffc",
        from_header="x\x00@mail.example",
        to_addr="t\x00@mail.example",
    )
    assert clamped.subject == "hi there?!"
    assert clamped.body_text == "ab?c"
    assert clamped.from_header == "x@mail.example"
    assert clamped.to_addr == "t@mail.example"
    for value in (clamped.subject, clamped.body_text, clamped.from_header):
        value.encode("utf-8")  # no lone surrogate is left


def test_an_identity_is_refused_not_rewritten_when_it_holds_a_nul() -> None:
    """Cleaning "a\\x00b" to "ab" could land on another message's id and overwrite it."""
    assert clamp_message(_message(message_id="a\x00b@mail.example")) is None
    assert clamp_message(_message(mailpit_id="id\ud800")) is None
    assert not limits.identity_storable("a\x00b", "id1")


def test_the_page_url_cap_matches_the_crm_source_column() -> None:
    from opskit.db.migrations.versions import crm_0003_bounds
    from opskit.leads.retrieval import MAX_PAGE_URL_CHARS

    assert crm_0003_bounds.ACCOUNT_SOURCES["source_ref"]["max"] == MAX_PAGE_URL_CHARS
