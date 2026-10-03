"""FastAPI application: service API for n8n under /v1, approver page under /approver."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from opskit.api.approver_session import LoginThrottle, SessionCodec, SessionStore
from opskit.api.middleware import SecurityHeaders
from opskit.api.routers import (
    approvals,
    approver,
    health,
    inbox,
    inbox_release,
    inputs,
    leads,
    receipts,
    reconcile,
    runs,
    smoke,
)
from opskit.api.routers.health import load_build_info
from opskit.approvals.resume import N8nResumeSender
from opskit.config import Settings
from opskit.core.factory import build_core, build_resume_worker
from opskit.core.ports import Core
from opskit.db.engine import make_engine, make_session_factory

log = logging.getLogger(__name__)
# httpx logs full request URLs at INFO; a resume URL's signature must never reach a log.
logging.getLogger("httpx").setLevel(logging.WARNING)
SWEEP_INTERVAL_S = 60


async def _sweep_expired(core: Core) -> None:
    while True:
        try:
            await core.approvals.expire_due(now=datetime.now(UTC))
        except Exception:
            log.exception("approval expiry sweep failed")
        await asyncio.sleep(SWEEP_INTERVAL_S)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings)
        session_factory = make_session_factory(engine)
        core = build_core(settings, session_factory)
        sender = N8nResumeSender(settings)
        app.state.settings = settings
        app.state.engine = engine
        app.state.core = core
        app.state.session_factory = session_factory
        if settings.leads_retrieval == "web":
            app.state.leads_web = leads.WebSession()
        app.state.service_token = settings.read_secret("api_service_token")
        app.state.approver_password_hash = settings.read_secret("approver_password.bcrypt").encode()
        app.state.session_codec = SessionCodec(
            settings.read_secret("approver_session_secret"),
            max_age_s=settings.approver_session_hours * 3600,
        )
        app.state.session_store = SessionStore(
            session_factory, lifetime=timedelta(hours=settings.approver_session_hours)
        )
        app.state.login_throttle = LoginThrottle()
        workers = [
            asyncio.create_task(build_resume_worker(session_factory, sender)),
            asyncio.create_task(_sweep_expired(core)),
        ]
        try:
            yield
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            await sender.aclose()
            web = getattr(app.state, "leads_web", None)
            if web is not None:
                await web.aclose()
            await engine.dispose()

    app = FastAPI(
        title="ops-automation-kit helper API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.build_info = load_build_info()
    app.add_middleware(SecurityHeaders)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    for module in (
        health,
        runs,
        approvals,
        smoke,
        inputs,
        inbox,
        inbox_release,
        leads,
        approver,
        receipts,
        reconcile,
    ):
        app.include_router(module.router)
    return app
