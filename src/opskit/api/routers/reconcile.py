"""Reconcile stored receipt extractions against the newest bank statement."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError

from opskit.api.auth import ServiceAuth
from opskit.config import Settings
from opskit.db.tables import receipt_reconciliations
from opskit.receipts.bank import parse_statement
from opskit.receipts.extraction import ExtractionOutcome
from opskit.receipts.reconcile import UNFLAGGED, ReceiptFacts, reconcile
from opskit.receipts.store import all_outcomes, session_factory_of

router = APIRouter(prefix="/v1/receipts", tags=["receipts"], dependencies=[ServiceAuth])


class ReconcileRequest(BaseModel):
    run_id: UUID


def facts_from_outcome(outcome: ExtractionOutcome) -> ReceiptFacts:
    fields = outcome.fields
    receipt_date = _parse_date(fields.receipt_date) if fields else None
    return ReceiptFacts(
        file=Path(outcome.path).name,
        vendor=fields.vendor_name if fields else None,
        receipt_date=receipt_date,
        total_cents=fields.total_cents if fields else None,
        needs_review=outcome.status != "extracted",
    )


def _parse_date(text: str | None) -> date | None:
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def newest_statement(settings: Settings) -> Path | None:
    folder = settings.samples_dir / "receipts" / "bank"
    return max(folder.glob("*.csv"), key=lambda p: p.name, default=None)


@router.post("/reconcile")
async def reconcile_receipts(request: Request, body: ReconcileRequest) -> dict[str, Any]:
    session_factory = session_factory_of(request.app)
    if session_factory is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable")
    statement = newest_statement(request.app.state.settings)
    if statement is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no bank statement found")
    try:
        bank = parse_statement(statement)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    outcomes = await all_outcomes(session_factory)
    result = reconcile([facts_from_outcome(o) for o in outcomes], bank)
    rows = [row.model_dump(mode="json") for row in result.rows]

    try:
        async with session_factory() as session, session.begin():
            await session.execute(
                insert(receipt_reconciliations).values(
                    run_id=body.run_id, rows=rows, summary=result.summary
                )
            )
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such run") from exc

    return {
        "run_id": str(body.run_id),
        "summary": result.summary,
        "rows": rows,
        "needs_review": result.needs_review,
        "flagged_rows": [row for row in rows if row["status"] not in UNFLAGGED],
    }
