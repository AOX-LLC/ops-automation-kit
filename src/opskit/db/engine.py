"""Async engine and session factory for the app database."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from opskit.config import Settings

type SessionFactory = async_sessionmaker[AsyncSession]


def make_engine(settings: Settings) -> AsyncEngine:
    """The requester role's engine: everything the n8n-facing API does."""
    return create_async_engine(
        settings.database_url(), pool_size=5, max_overflow=5, pool_pre_ping=True
    )


def make_approver_engine(settings: Settings) -> AsyncEngine:
    """The approver role's engine, for the approver page's decision path only."""
    return create_async_engine(
        settings.database_url(approver=True), pool_size=2, max_overflow=2, pool_pre_ping=True
    )


def make_session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False)
