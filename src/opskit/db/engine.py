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
    return create_async_engine(
        settings.database_url(), pool_size=5, max_overflow=5, pool_pre_ping=True
    )


def make_session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False)
