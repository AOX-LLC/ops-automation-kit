"""Run records: one per n8n execution, idempotent on the execution id."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from aox_agent_core.context import RunContext
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from opskit.core.errors import NotFound, RunInProgress
from opskit.core.ports import Mode
from opskit.db.engine import SessionFactory
from opskit.db.tables import runs


def _context(row: Any) -> RunContext:
    """agent-core's RunContext: the run id as a string, n8n's ids as external ids."""
    external = {
        name: value
        for name, value in (
            ("workflow", row.workflow),
            ("n8n_workflow_id", row.n8n_workflow_id),
            ("n8n_execution_id", row.n8n_execution_id),
        )
        if value
    }
    return RunContext(run_id=str(row.id), external_ids=external)


# A run still marked running after this long is taken to have died, so it stops blocking the next.
# Replay runs take about a minute. A failure inside n8n is closed at once by the error workflow
# (05-run-error.json); this window only matters when that workflow fails too, or n8n dies mid-run.
STALE_AFTER = timedelta(minutes=15)
# The smoke workflow waits for a human approval for up to its lifetime, so it may overlap itself.
SINGLE_FLIGHT = frozenset({"receipts", "leads", "inbox"})


class PgRunStore:
    def __init__(self, session_factory: SessionFactory, mode: Mode) -> None:
        self._session_factory = session_factory
        self._mode = mode

    async def start(
        self, *, workflow: str, n8n_workflow_id: str | None, n8n_execution_id: str | None
    ) -> RunContext:
        # An empty id means none: stored as '' two runs would share the UNIQUE execution id.
        n8n_workflow_id = n8n_workflow_id or None
        n8n_execution_id = n8n_execution_id or None
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
            if workflow in SINGLE_FLIGHT:
                await self._refuse_overlap(session, workflow, n8n_execution_id)
            row = (await session.execute(statement)).one()
        return _context(row)

    @staticmethod
    async def _refuse_overlap(session: Any, workflow: str, n8n_execution_id: str | None) -> None:
        """One run of a workflow at a time: the lock makes two simultaneous starts take turns, so
        the second sees the first. A retry of the same n8n execution is not an overlap."""
        await session.execute(
            text("select pg_advisory_xact_lock(hashtext(:key))"), {"key": f"run:{workflow}"}
        )
        conditions = [
            runs.c.workflow == workflow,
            runs.c.status == "running",
            runs.c.started_at > func.now() - STALE_AFTER,
        ]
        if n8n_execution_id is not None:  # with no id there is no retry to recognise
            conditions.append(runs.c.n8n_execution_id.is_distinct_from(n8n_execution_id))
        busy = (
            await session.execute(select(runs.c.id).where(*conditions).limit(1))
        ).scalar_one_or_none()
        if busy is not None:
            raise RunInProgress(workflow, busy)

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

    async def fail_by_execution(self, n8n_execution_id: str) -> bool:
        """Close the still-running run of a failed n8n execution. False when there is none: the
        failure came before the run started, or the run had already finished."""
        statement = (
            update(runs)
            .where(runs.c.n8n_execution_id == n8n_execution_id, runs.c.status == "running")
            .values(status="failed", finished_at=func.now())
            .returning(runs.c.id)
        )
        async with self._session_factory.begin() as session:
            return (await session.execute(statement)).first() is not None
