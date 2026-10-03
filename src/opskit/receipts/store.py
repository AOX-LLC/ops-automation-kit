"""Persistence for extraction outcomes, keyed by the SHA-256 of the original file."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from opskit.db.engine import SessionFactory, make_session_factory
from opskit.db.tables import receipt_extractions as extractions
from opskit.receipts.extraction import ExtractionOutcome, ReceiptExtraction

_REPLACEABLE = (
    "sha256",
    "run_id",
    "status",
    "reason",
    "fields",
    "replay_key",
    "tier",
    "model",
    "cost_usd",
    "latency_ms",
)


def session_factory_of(app: FastAPI) -> SessionFactory | None:
    """The app's session factory, built from its engine; None when it has no database."""
    factory: SessionFactory | None = getattr(app.state, "session_factory", None)
    if factory is not None:
        return factory
    engine = getattr(app.state, "engine", None)
    return make_session_factory(engine) if engine is not None else None


def _values(run_id: UUID, outcome: ExtractionOutcome) -> dict[str, Any]:
    return {
        "sha256": outcome.sha256,
        "path": outcome.path,
        "run_id": run_id,
        "status": outcome.status,
        "reason": outcome.reason,
        "fields": outcome.fields.model_dump(mode="json") if outcome.fields else None,
        "replay_key": outcome.replay_key,
        "tier": outcome.tier,
        "model": outcome.model,
        "cost_usd": Decimal(outcome.cost_usd),
        "latency_ms": outcome.latency_ms,
    }


async def save_outcome(
    session_factory: SessionFactory, run_id: UUID, outcome: ExtractionOutcome
) -> None:
    """Insert the outcome, or replace the earlier one for the same file path."""
    statement = insert(extractions).values(_values(run_id, outcome))
    statement = statement.on_conflict_do_update(
        index_elements=[extractions.c.path],
        set_={name: statement.excluded[name] for name in _REPLACEABLE}
        | {"extracted_at": statement.excluded.extracted_at},
    )
    async with session_factory() as session, session.begin():
        await session.execute(statement)


async def processed_paths(session_factory: SessionFactory) -> set[str]:
    """Public paths already extracted (or flagged), so the pending list can skip them."""
    async with session_factory() as session:
        rows = await session.execute(select(extractions.c.path))
    return {row[0] for row in rows}


async def processed_hashes(session_factory: SessionFactory) -> set[str]:
    async with session_factory() as session:
        rows = await session.execute(select(extractions.c.sha256))
        return {sha.strip() for sha in rows.scalars()}


async def all_outcomes(session_factory: SessionFactory) -> list[ExtractionOutcome]:
    async with session_factory() as session:
        rows = await session.execute(select(extractions).order_by(extractions.c.path))
        return [
            ExtractionOutcome(
                path=row.path,
                sha256=row.sha256,
                status=row.status,
                reason=row.reason,
                fields=ReceiptExtraction.model_validate(row.fields) if row.fields else None,
                replay_key=row.replay_key.strip() if row.replay_key else None,
                tier=row.tier,
                model=row.model,
                cost_usd=format(row.cost_usd, "f"),
                latency_ms=row.latency_ms,
            )
            for row in rows
        ]
