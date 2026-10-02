"""The receipts sample set must regenerate byte-for-byte and its answer key must be coherent."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from tools.samplegen import receipts

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLES = REPO_ROOT / "samples" / "receipts"
KEYS = REPO_ROOT / "evals" / "answer_keys" / "receipts"
ANSWER_KEY_FIELD_NAMES = (b"expected_", b"status", b"category", b"priority")
REAL_TLD_PATTERN = re.compile(r"\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|io|co|biz|info|us|dev)\b")


def _files_under(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _load_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = json.loads((KEYS / "reconciliation.json").read_text())["rows"]
    return rows


@pytest.fixture(scope="module")
def regenerated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out_root = tmp_path_factory.mktemp("regen")
    receipts.generate(out_root)
    return out_root


@pytest.mark.parametrize(
    "relative_root",
    ["samples/receipts", "evals/answer_keys/receipts"],
)
def test_regeneration_matches_committed_files_byte_for_byte(
    regenerated: Path, relative_root: str
) -> None:
    fresh = _files_under(regenerated / relative_root)
    committed = _files_under(REPO_ROOT / relative_root)
    assert fresh.keys() == committed.keys()
    assert [name for name in fresh if fresh[name] != committed[name]] == []


def test_inbox_has_26_pngs_and_4_pdfs() -> None:
    inbox = sorted(path.name for path in (SAMPLES / "inbox").iterdir())
    assert len(inbox) == 30
    assert sum(name.endswith(".png") for name in inbox) == 26
    assert sum(name.endswith(".pdf") for name in inbox) == 4
    assert [name.split(".")[0] for name in inbox] == [f"r{n:02d}" for n in range(1, 31)]


def test_status_counts_match_target_mix() -> None:
    assert dict(Counter(row["status"] for row in _load_rows())) == receipts.TARGET_MIX


def test_every_bank_reference_and_receipt_file_appears_in_exactly_one_row() -> None:
    rows = _load_rows()
    with (SAMPLES / "bank" / "statement-2026-08.csv").open(newline="") as handle:
        references = [row["reference"] for row in csv.DictReader(handle)]
    in_rows = Counter(row["bank_reference"] for row in rows if row["bank_reference"])
    assert in_rows == Counter(references)
    assert set(in_rows.values()) == {1}

    files = sorted(path.name for path in (SAMPLES / "inbox").iterdir())
    receipt_rows = Counter(row["receipt"] for row in rows if row["receipt"])
    assert receipt_rows == Counter(files)
    assert set(receipt_rows.values()) == {1}


def test_label_rules_hold_for_every_row() -> None:
    for row in _load_rows():
        if row["status"] == "matched":
            assert 0 <= row["delta_days"] <= receipts.MAX_MATCHED_LAG_DAYS
            assert row["delta_cents"] == 0
        if row["status"] == "date_drift":
            assert row["delta_days"] > receipts.MAX_MATCHED_LAG_DAYS
        if row["status"] == "amount_mismatch":
            assert row["delta_cents"] != 0


def test_reconciliation_documents_its_rules() -> None:
    rules = json.loads((KEYS / "reconciliation.json").read_text())["rules"]
    assert set(receipts.TARGET_MIX) <= set(rules)


def test_receipt_totals_add_up_in_integer_cents() -> None:
    entries = json.loads((KEYS / "receipts.json").read_text())["receipts"]
    assert len(entries) == 30
    for entry in entries:
        assert all(isinstance(entry[key], int) for key in entry if key.endswith("_cents"))
        assert entry["subtotal_cents"] == sum(i["line_total_cents"] for i in entry["line_items"])
        assert entry["total_cents"] == (
            entry["subtotal_cents"] + entry["tax_cents"] + entry["tip_cents"]
        )
    assert sum(entry["is_refund"] for entry in entries) == 1


def test_bank_csv_uses_plain_two_place_decimals_with_negative_debits() -> None:
    with (SAMPLES / "bank" / "statement-2026-08.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0]) == ["posted_date", "description", "amount", "balance", "reference"]
    for row in rows:
        assert re.fullmatch(r"-?\d+\.\d{2}", row["amount"])
        assert re.fullmatch(r"-?\d+\.\d{2}", row["balance"])
    assert any(row["amount"].startswith("-") for row in rows)


def test_no_answer_key_field_name_appears_under_samples() -> None:
    leaks = [
        (name, marker.decode())
        for name, content in _files_under(SAMPLES).items()
        for marker in ANSWER_KEY_FIELD_NAMES
        if marker in content
    ]
    assert leaks == []


def test_every_domain_ends_in_example_and_phones_are_fictional() -> None:
    spec = yaml.safe_load(receipts.SPEC_PATH.read_text())
    assert spec["vendors"]
    for vendor in spec["vendors"].values():
        assert vendor["domain"].endswith(".example")
        assert re.fullmatch(r"555-01\d\d", vendor["phone"])
    published_text = [
        receipts.SPEC_PATH.read_text(),
        (KEYS / "receipts.json").read_text(),
        (KEYS / "reconciliation.json").read_text(),
        (SAMPLES / "bank" / "statement-2026-08.csv").read_text(),
    ]
    for text in published_text:
        assert "@" not in text
        assert REAL_TLD_PATTERN.findall(text) == []
