from __future__ import annotations

import hashlib
from pathlib import Path

from opskit.seed.crm import parse_accounts
from opskit.seed.manifest import build_manifest


def _write(root: Path, relative: str, data: bytes = b"x") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_build_manifest_classifies_sorts_and_hashes(tmp_path: Path) -> None:
    _write(tmp_path, "receipts/inbox/b.pdf", b"pdf")
    _write(tmp_path, "receipts/inbox/a.png", b"png")
    _write(tmp_path, "receipts/bank/s.csv", b"bank")
    _write(tmp_path, "leads/companies.csv", b"c")
    _write(tmp_path, "leads/corpus/acme.example/about.html", b"h")
    _write(tmp_path, "crm/accounts.csv", b"crm")
    _write(tmp_path, "inbox/messages/m1.eml", b"mail")
    _write(tmp_path, "inbox/business_profile.md", b"profile")
    _write(tmp_path, "README.md", b"ignored")

    rows = build_manifest(tmp_path)

    assert [r["path"] for r in rows] == sorted(r["path"] for r in rows)
    kinds = {r["path"]: (r["workflow"], r["kind"]) for r in rows}
    assert kinds == {
        "crm/accounts.csv": ("leads", "crm_accounts"),
        "inbox/business_profile.md": ("inbox", "business_profile"),
        "inbox/messages/m1.eml": ("inbox", "email"),
        "leads/companies.csv": ("leads", "company_list"),
        "leads/corpus/acme.example/about.html": ("leads", "corpus_doc"),
        "receipts/bank/s.csv": ("receipts", "bank_csv"),
        "receipts/inbox/a.png": ("receipts", "receipt_image"),
        "receipts/inbox/b.pdf": ("receipts", "receipt_image"),
    }
    first = rows[-2]
    assert first["path"] == "receipts/inbox/a.png"
    assert first["sha256"] == hashlib.sha256(b"png").hexdigest()
    assert first["bytes"] == 3


def test_parse_accounts(tmp_path: Path) -> None:
    csv_path = tmp_path / "accounts.csv"
    csv_path.write_text(
        "name,domain,industry,employee_band,hq_city,description\n"
        'Acme Co,acme.example,Retail,11-50,Springfield,"Sells, mostly"\n',
        encoding="utf-8",
    )

    assert parse_accounts(csv_path) == [
        {
            "name": "Acme Co",
            "domain": "acme.example",
            "industry": "Retail",
            "employee_band": "11-50",
            "hq_city": "Springfield",
            "description": "Sells, mostly",
        }
    ]
