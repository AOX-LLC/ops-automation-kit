"""Sample-file manifest: what ships under samples/, with hashes, loaded into core.sample_files."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TypedDict

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from opskit.db.tables import sample_files


class ManifestRow(TypedDict):
    path: str
    workflow: str
    kind: str
    sha256: str
    bytes: int


def classify(relative: Path) -> tuple[str, str] | None:
    """Return (workflow, kind) for a samples-relative path, or None for files we do not track."""
    parts = relative.parts
    suffix = relative.suffix.lower()
    if parts[:2] == ("receipts", "inbox") and suffix in {".png", ".pdf"}:
        return "receipts", "receipt_image"
    if parts[:2] == ("receipts", "bank") and suffix == ".csv":
        return "receipts", "bank_csv"
    if relative.as_posix() == "leads/companies.csv":
        return "leads", "company_list"
    if parts[:2] == ("leads", "corpus"):
        return "leads", "corpus_doc"
    if relative.as_posix() == "crm/accounts.csv":
        return "leads", "crm_accounts"
    if parts[:2] == ("inbox", "messages") and suffix == ".eml":
        return "inbox", "email"
    if relative.as_posix() == "inbox/business_profile.md":
        return "inbox", "business_profile"
    return None


def build_manifest(root: Path) -> list[ManifestRow]:
    rows: list[ManifestRow] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root)
        classified = classify(relative)
        if classified is None:
            continue
        data = path.read_bytes()
        rows.append(
            ManifestRow(
                path=relative.as_posix(),
                workflow=classified[0],
                kind=classified[1],
                sha256=hashlib.sha256(data).hexdigest(),
                bytes=len(data),
            )
        )
    return rows


async def upsert_manifest(session: AsyncSession, rows: list[ManifestRow]) -> int:
    if not rows:
        return 0
    statement = insert(sample_files)
    statement = statement.on_conflict_do_update(
        index_elements=[sample_files.c.path],
        set_={
            "workflow": statement.excluded.workflow,
            "kind": statement.excluded.kind,
            "sha256": statement.excluded.sha256,
            "bytes": statement.excluded.bytes,
        },
    )
    await session.execute(statement, rows)
    return len(rows)
