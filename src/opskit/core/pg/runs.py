"""Run records: one per n8n execution, idempotent on the execution id."""

from __future__ import annotations

from types import MappingProxyType
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from opskit.core.errors import NotFound
from opskit.core.ports import Mode, RunContext
from opskit.db.engine import SessionFactory
from opskit.db.tables import runs


def _context(row: object) -> RunContext:
    external = {
        key: value
        for key, value in (
            ("n8n_workflow_id", getattr(row, "n8n_workflow_id", None)),
            ("n8n_execution_id", getattr(row, "n8n_execution_id", None)),
        )
        if value
    }
    return RunContext(
        run_id=row.id,  # type: ignore[attr-defined]
        workflow=row.workflow,  # type: ignore[attr-defined]
        mode=Mode(row.mode),  # type: ignore[attr-defined]
        external_ids=MappingProxyType(external),
    )


class PgRunStore:
    def __init__(self, session_factory: SessionFactory, mode: Mode) -> None:
        self._session_factory = session_factory
        self._mode = mode

    async def start(
        self, *, workflow: str, n8n_workflow_id: str | None, n8n_execution_id: str | None
    ) -> RunContext:
        statement = (
            insert(runs)
            .values(
                workflow=workflow,
                n8n_workflow_id=n8n_workflow_id,
                n8n_execution_id=n8n_execution_id,
                mode=self._mode.value,
            )
            .on_conflict_do_update(
                index_elements=[runs.c.n8n_execution_id],
                set_={"n8n_workflow_id": n8n_workflow_id},
            )
            .returning(runs)
        )
        async with self._session_factory.begin() as session:
            row = (await session.execute(statement)).one()
        return _context(row)

    async def get(self, run_id: UUID) -> RunContext:
        async with self._session_factory() as session:
            row = (await session.execute(select(runs).where(runs.c.id == run_id))).one_or_none()
        if row is None:
            raise NotFound("run", run_id)
        return _context(row)

    async def finish(self, run_id: UUID, *, succeeded: bool) -> None:
        statement = (
            update(runs)
            .where(runs.c.id == run_id)
            .values(status="succeeded" if succeeded else "failed", finished_at=func.now())
            .returning(runs.c.id)
        )
        async with self._session_factory.begin() as session:
            if (await session.execute(statement)).one_or_none() is None:
                raise NotFound("run", run_id)
