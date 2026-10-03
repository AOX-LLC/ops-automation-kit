"""Receipt extraction for n8n: one receipt per call, saved by content hash."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from opskit.api.auth import ServiceAuth
from opskit.api.routers.inputs import collect_receipts
from opskit.config import Settings
from opskit.core.errors import NotFound, ReplayMissError
from opskit.core.ports import Core
from opskit.receipts.extraction import ExtractionOutcome, extract_receipt
from opskit.receipts.store import save_outcome, session_factory_of

router = APIRouter(prefix="/v1/receipts", tags=["receipts"], dependencies=[ServiceAuth])


class ExtractRequest(BaseModel):
    run_id: UUID
    path: str = Field(max_length=512, description="A path from GET /v1/receipts/pending")


async def _listed_file(settings: Settings, public_path: str) -> Path:
    """The listed file for a public path. Input is only ever compared, never joined."""
    listed = await asyncio.to_thread(collect_receipts, settings)
    for file in listed:
        if file.public_path == public_path:
            return file.path
    raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown receipt path")


@router.post("/extract")
async def extract(request: Request, body: ExtractRequest) -> ExtractionOutcome:
    settings: Settings = request.app.state.settings
    core: Core = request.app.state.core
    file_path = await _listed_file(settings, body.path)
    try:
        ctx = await core.runs.get(body.run_id)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found") from exc
    session_factory = session_factory_of(request.app)
    if session_factory is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable")
    try:
        outcome = await extract_receipt(core.models, ctx, file_path, public_path=body.path)
    except ReplayMissError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"no recording for replay key {exc.key or 'unknown'}",
        ) from exc
    await save_outcome(session_factory, body.run_id, outcome)
    return outcome
