"""Append-only audit log in Postgres (the database also rejects updates and deletes)."""

from __future__ import annotations

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from opskit.core.ports import JsonObject, RunContext
from opskit.db.engine import SessionFactory
from opskit.db.tables import audit_log


async def append_in(
    session: AsyncSession,
    *,
    ctx: RunContext | None,
    actor: str,
    action: str,
    subject_type: str | None,
    subject_id: str | None,
    details: JsonObject,
) -> None:
    """Write an audit row in the caller's transaction, so it commits with the change it records."""
    await session.execute(
        insert(audit_log).values(
            run_id=ctx.run_id if ctx else None,
            actor=actor,
            action=action,
            subject_type=subject_type,
            subject_id=subject_id,
            details=details,
        )
    )


class PgAuditLog:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def append(
        self,
        *,
        ctx: RunContext | None,
        actor: str,
        action: str,
        subject_type: str | None,
        subject_id: str | None,
        details: JsonObject,
    ) -> None:
        async with self._session_factory.begin() as session:
            await append_in(
                session,
                ctx=ctx,
                actor=actor,
                action=action,
                subject_type=subject_type,
                subject_id=subject_id,
                details=details,
            )
