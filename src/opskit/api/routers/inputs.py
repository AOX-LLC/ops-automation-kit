"""Read-only listings of the files and messages the workflow skeletons process."""

from __future__ import annotations

import asyncio
import base64
import binascii
import csv
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from opskit.api.auth import ServiceAuth
from opskit.config import Settings
from opskit.receipts.extraction import sha256_of_file
from opskit.receipts.store import processed_paths, session_factory_of

router = APIRouter(prefix="/v1", tags=["inputs"], dependencies=[ServiceAuth])

RECEIPT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".pdf": "application/pdf",
}
MAILPIT_TIMEOUT_S = 5.0
CURSOR_PATTERN = re.compile(r"[A-Za-z0-9_-]+")

Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(max_length=512)]


class ReceiptItem(BaseModel):
    path: str
    sha256: str
    media_type: str
    source: str


class LeadItem(BaseModel):
    company_name: str
    city_hint: str


class InboxItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mailpit_id: str
    message_id: str
    from_: str = Field(alias="from")
    subject: str
    received_at: str


class ReceiptPage(BaseModel):
    items: list[ReceiptItem]
    next_cursor: str | None


class LeadPage(BaseModel):
    items: list[LeadItem]
    next_cursor: str | None


class InboxPage(BaseModel):
    items: list[InboxItem]
    next_cursor: str | None


def encode_cursor(key: str) -> str:
    return base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> str | None:
    if cursor is None:
        return None
    if not CURSOR_PATTERN.fullmatch(cursor):  # b64decode would silently drop bad characters
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode()
    except (binascii.Error, UnicodeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid cursor") from exc


def decode_offset(cursor: str | None) -> int:
    raw = decode_cursor(cursor)
    if raw is None:
        return 0
    if not raw.isdecimal():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid cursor")
    return int(raw)


@dataclass(frozen=True)
class ReceiptFile:
    source: str
    name: str
    path: Path  # already resolved and verified to sit inside its root
    public_path: str

    @property
    def key(self) -> str:
        return f"{self.source}/{self.name}"


def list_receipt_files(root: Path, source: str, public_prefix: str) -> list[ReceiptFile]:
    """Receipt files directly inside `root`; symlinks and anything escaping root are skipped."""
    if not root.is_dir():
        return []
    resolved_root = root.resolve()
    found: list[ReceiptFile] = []
    for entry in root.iterdir():
        if entry.suffix.lower() not in RECEIPT_TYPES or entry.is_symlink() or not entry.is_file():
            continue
        resolved = entry.resolve()
        if resolved.parent != resolved_root:
            continue
        found.append(ReceiptFile(source, entry.name, resolved, f"{public_prefix}/{entry.name}"))
    return found


def collect_receipts(settings: Settings) -> list[ReceiptFile]:
    files = list_receipt_files(
        settings.samples_dir / "receipts" / "inbox", "samples", "receipts/inbox"
    )
    files += list_receipt_files(settings.dropbox_dir / "receipts", "dropbox", "dropbox/receipts")
    return sorted(files, key=lambda f: f.key)


def page_after_key[T](
    items: list[T], key_of: Callable[[T], str], cursor_key: str | None, limit: int
) -> tuple[list[T], str | None]:
    """Keyset page over items sorted by key_of: strictly after cursor_key, `limit` long."""
    remaining = [i for i in items if cursor_key is None or key_of(i) > cursor_key]
    page = remaining[:limit]
    more = len(remaining) > limit
    return page, encode_cursor(key_of(page[-1])) if more and page else None


def receipt_item(file: ReceiptFile, sha256: str) -> ReceiptItem:
    return ReceiptItem(
        path=file.public_path,
        sha256=sha256,
        media_type=RECEIPT_TYPES[file.path.suffix.lower()],
        source=file.source,
    )


def read_companies(csv_path: Path) -> list[LeadItem]:
    if not csv_path.is_file():
        return []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        return [
            LeadItem(company_name=row["company_name"], city_hint=row["city_hint"])
            for row in csv.DictReader(handle)
        ]


def page_by_offset[T](items: list[T], offset: int, limit: int) -> tuple[list[T], str | None]:
    page = items[offset : offset + limit]
    more = offset + limit < len(items)
    return page, encode_cursor(str(offset + limit)) if more else None


def inbox_item(raw: dict[str, Any]) -> InboxItem:
    sender = raw.get("From") or {}
    return InboxItem(
        mailpit_id=str(raw.get("ID", "")),
        message_id=str(raw.get("MessageID", "")),
        from_=str(sender.get("Address", "")),
        subject=str(raw.get("Subject", "")),
        received_at=str(raw.get("Created", "")),
    )


HASH_CACHE_MAX_ENTRIES = 4096
_hash_cache: dict[tuple[Path, int, int], str] = {}
_hash_cache_lock = threading.Lock()


def _file_sha256(path: Path) -> str:
    """Chunked SHA-256, cached by (path, size, mtime) so an unchanged file is read once."""
    info = path.stat()
    key = (path, info.st_size, info.st_mtime_ns)
    with _hash_cache_lock:
        cached = _hash_cache.get(key)
    if cached is not None:
        return cached
    digest = sha256_of_file(path)
    with _hash_cache_lock:
        _hash_cache[key] = digest
        while len(_hash_cache) > HASH_CACHE_MAX_ENTRIES:
            del _hash_cache[next(iter(_hash_cache))]
    return digest


def _hash_files(files: list[ReceiptFile]) -> list[tuple[ReceiptFile, str]]:
    return [(f, _file_sha256(f.path)) for f in files]


@router.get("/receipts/pending")
async def pending_receipts(
    request: Request,
    limit: Limit = 50,
    cursor: Cursor = None,
    include_processed: bool = False,
) -> ReceiptPage:
    """Receipt files not yet extracted (by public path); `include_processed` lists all."""
    settings: Settings = request.app.state.settings
    cursor_key = decode_cursor(cursor)
    files = await asyncio.to_thread(collect_receipts, settings)
    hashed = await asyncio.to_thread(_hash_files, files)
    session_factory = None if include_processed else session_factory_of(request.app)
    if session_factory is not None:
        done = await processed_paths(session_factory)
        hashed = [(f, sha) for f, sha in hashed if f.public_path not in done]
    page, next_cursor = page_after_key(hashed, lambda pair: pair[0].key, cursor_key, limit)
    return ReceiptPage(
        items=[receipt_item(f, sha) for f, sha in page],
        next_cursor=next_cursor,
    )


@router.get("/leads/pending")
def pending_leads(request: Request, limit: Limit = 50, cursor: Cursor = None) -> LeadPage:
    settings: Settings = request.app.state.settings
    companies = read_companies(settings.samples_dir / "leads" / "companies.csv")
    items, next_cursor = page_by_offset(companies, decode_offset(cursor), limit)
    return LeadPage(items=items, next_cursor=next_cursor)


async def fetch_mailpit_page(
    client: httpx.AsyncClient, api_url: str, start: int, limit: int
) -> dict[str, Any]:
    response = await client.get(
        f"{api_url}/api/v1/messages", params={"start": start, "limit": limit}
    )
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    return body


@router.get("/inbox/pending")
async def pending_inbox(request: Request, limit: Limit = 50, cursor: Cursor = None) -> InboxPage:
    settings: Settings = request.app.state.settings
    start = decode_offset(cursor)
    shared: httpx.AsyncClient | None = getattr(request.app.state, "http_client", None)
    try:
        if shared is not None:
            body = await fetch_mailpit_page(shared, settings.mailpit_api_url, start, limit)
        else:
            async with httpx.AsyncClient(timeout=MAILPIT_TIMEOUT_S) as client:
                body = await fetch_mailpit_page(client, settings.mailpit_api_url, start, limit)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "mailpit unavailable") from exc
    items = [inbox_item(m) for m in body.get("messages", [])]
    total = int(body.get("messages_count", body.get("total", 0)))
    more = start + len(items) < total and bool(items)
    return InboxPage(
        items=items, next_cursor=encode_cursor(str(start + len(items))) if more else None
    )
