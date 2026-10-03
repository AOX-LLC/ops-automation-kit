"""Leads sample data: reproducibility, answer-key integrity and fictional-only content."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

import pytest

from tools.samplegen import leads

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATED_ROOTS = ("samples/leads", "samples/crm", "evals/answer_keys/leads")
ANSWER_KEY_MARKERS = ("expected_", "crm_action", "conflict")
HOSTNAME = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
EMAIL = re.compile(r"[\w.+-]+@([\w.-]+)")


def _files(root: Path, *subdirs: str) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for sub in subdirs
        for path in sorted((root / sub).rglob("*"))
        if path.is_file()
    }


@pytest.fixture(scope="module")
def key() -> dict[str, Any]:
    path = REPO_ROOT / "evals/answer_keys/leads/expected_records.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _cited(key: dict[str, Any]) -> list[tuple[str, str, Any, str]]:
    cited = []
    for name, record in key.items():
        for field_name, entry in record["fields"].items():
            if entry["value"] is not None:
                cited.append((name, field_name, entry["value"], entry["source"]))
            for candidate in entry.get("candidates", []):
                cited.append((name, field_name, candidate["value"], candidate["source"]))
    return cited


def test_regeneration_is_byte_identical(tmp_path: Path) -> None:
    leads.generate(tmp_path)
    assert _files(tmp_path, *GENERATED_ROOTS) == _files(REPO_ROOT, *GENERATED_ROOTS)


def test_twenty_companies_in_input_and_key(key: dict[str, Any]) -> None:
    with (REPO_ROOT / "samples/leads/companies.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 20
    assert list(rows[0]) == ["company_name", "city_hint", "website"]
    assert [row["company_name"] for row in rows] == sorted(row["company_name"] for row in rows)
    assert {row["company_name"] for row in rows} == set(key)


def test_every_cited_source_exists_and_contains_the_value(key: dict[str, Any]) -> None:
    cited = _cited(key)
    assert cited
    for name, field_name, value, source in cited:
        text = (REPO_ROOT / source).read_text(encoding="utf-8").lower()
        assert str(value).lower() in text, f"{name}.{field_name} not in {source}"


def test_companies_without_docs_have_all_null_fields(key: dict[str, Any]) -> None:
    empty = [r for r in key.values() if all(f["value"] is None for f in r["fields"].values())]
    assert len(empty) == 3
    assert all(f["source"] is None for r in empty for f in r["fields"].values())


def test_deliberate_cases_are_present(key: dict[str, Any]) -> None:
    conflicts = [n for n, r in key.items() if r["fields"]["employee_band"].get("conflict")]
    assert len(conflicts) == 2
    for name in conflicts:
        assert len(key[name]["fields"]["employee_band"]["candidates"]) == 2
    assert [n for n, r in key.items() if r["crm_action"] == "update"] == ["Ironwood Collision"]
    corpus = " ".join(
        p.read_text(encoding="utf-8") for p in (REPO_ROOT / "samples/leads").rglob("*.html")
    )
    assert corpus.count("Ignore previous instructions") == 1


def test_only_example_domains_and_emails() -> None:
    for root in ("samples/leads", "samples/crm"):
        for path in (REPO_ROOT / root).rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            assert all(h.endswith(".example") for h in HOSTNAME.findall(text)), path
            assert all(d.endswith(".example") for d in EMAIL.findall(text)), path


def test_no_answer_key_names_leak_into_samples() -> None:
    for root in ("samples/leads", "samples/crm"):
        for path in (REPO_ROOT / root).rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                assert not [m for m in ANSWER_KEY_MARKERS if m in text], path


def test_website_column_matches_the_key_outcome(key: dict[str, Any]) -> None:
    with (REPO_ROOT / "samples/leads/companies.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    blank = {row["company_name"] for row in rows if not row["website"]}
    assert len(blank) == 3
    for row in rows:
        if row["website"]:
            assert row["website"].endswith(".example")
            assert (REPO_ROOT / "samples/leads/corpus" / row["website"]).is_dir()
    for name, record in key.items():
        assert record["outcome"] == ("no website given" if name in blank else "researched")
