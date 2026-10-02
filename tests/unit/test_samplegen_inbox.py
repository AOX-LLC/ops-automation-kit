"""Pins the inbox sample set: reproducible, correctly labelled, fictional and leak-free."""

from __future__ import annotations

import email
import json
from collections import Counter
from pathlib import Path

import pytest

from tools.samplegen import inbox

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLES = REPO_ROOT / "samples" / "inbox"
KEY_PATH = REPO_ROOT / "evals" / "answer_keys" / "inbox" / "triage.json"
EXPECTED_COUNTS = {
    "sales_inquiry": 4,
    "support": 4,
    "billing": 3,
    "scheduling": 3,
    "complaint": 2,
    "vendor_invoice": 2,
    "spam_phishing": 2,
    "auto_reply": 2,
    "newsletter": 1,
    "other": 2,
}
LABEL_WORDS = ("category", "priority", "needs_reply", "escalate", "reply_must")


@pytest.fixture(scope="module")
def key() -> list[dict[str, object]]:
    data: list[dict[str, object]] = json.loads(KEY_PATH.read_text(encoding="utf-8"))
    return data


def inbox_files(root: Path) -> list[Path]:
    return sorted(path for path in (root / "samples" / "inbox").rglob("*") if path.is_file())


def test_regeneration_is_byte_identical(tmp_path: Path) -> None:
    inbox.generate(tmp_path)
    regenerated = [*inbox_files(tmp_path), tmp_path / "evals/answer_keys/inbox/triage.json"]
    assert len(regenerated) == 25 + 2
    for path in regenerated:
        committed = REPO_ROOT / path.relative_to(tmp_path)
        assert path.read_bytes() == committed.read_bytes(), path.name


def test_message_ids_are_unique(key: list[dict[str, object]]) -> None:
    ids = [str(entry["message_id"]) for entry in key]
    assert len(ids) == len(set(ids)) == 25
    assert all(message_id.endswith("@kit.example>") for message_id in ids)


def test_category_counts(key: list[dict[str, object]]) -> None:
    assert Counter(str(entry["category"]) for entry in key) == EXPECTED_COUNTS


def test_required_facts_appear_in_profile(key: list[dict[str, object]]) -> None:
    profile = (SAMPLES / "business_profile.md").read_text(encoding="utf-8")
    for entry in key:
        facts = entry["reply_must_include"]
        assert isinstance(facts, list)
        for fact in facts:
            assert fact in profile, (entry["file"], fact)


def test_priorities_are_valid(key: list[dict[str, object]]) -> None:
    assert {entry["priority"] for entry in key} <= {"low", "normal", "high", "urgent"}


def test_every_address_is_example_domain() -> None:
    for path in sorted((SAMPLES / "messages").glob("*.eml")):
        parsed = email.message_from_bytes(path.read_bytes())
        for header in ("From", "To"):
            addresses = email.utils.getaddresses([str(parsed[header])])
            assert addresses
            assert all(address.endswith(".example") for _, address in addresses), path.name


def test_eml_files_parse_with_expected_headers(key: list[dict[str, object]]) -> None:
    for entry in key:
        raw = (SAMPLES / "messages" / str(entry["file"])).read_bytes()
        assert b"\r\n" in raw
        assert b"\n" not in raw.replace(b"\r\n", b"")
        parsed = email.message_from_bytes(raw)
        assert parsed["Message-ID"] == entry["message_id"]
        for header in ("Date", "From", "To", "Subject", "MIME-Version"):
            assert parsed[header], (entry["file"], header)
        assert parsed.get_content_type() == "text/plain"
        assert parsed.get_content_charset() == "utf-8"
        assert "2026" in str(parsed["Date"]) and "Aug" in str(parsed["Date"])


def test_one_message_replies_in_a_thread() -> None:
    replies = [
        path
        for path in (SAMPLES / "messages").glob("*.eml")
        if email.message_from_bytes(path.read_bytes())["In-Reply-To"]
    ]
    assert len(replies) == 1


def test_no_label_field_names_leak_into_samples() -> None:
    for path in inbox_files(REPO_ROOT):
        text = path.read_text(encoding="utf-8").lower()
        for word in LABEL_WORDS:
            assert word not in text, (path.name, word)
