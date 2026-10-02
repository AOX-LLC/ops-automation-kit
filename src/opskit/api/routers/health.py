"""Liveness and readiness probes. /healthz also reports what build is running."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import text

router = APIRouter(tags=["health"])
log = logging.getLogger(__name__)

SCHEMA_QUERY_TIMEOUT_S = 1.0


@dataclass(frozen=True)
class BuildInfo:
    """Identity of the running process, read once at start."""

    version: str
    commit: str | None
    branch: str | None
    started_monotonic: float


def load_build_info() -> BuildInfo:
    return BuildInfo(
        version=version("opskit"),
        commit=os.environ.get("OPSKIT_BUILD_COMMIT") or None,
        branch=os.environ.get("OPSKIT_BUILD_BRANCH") or None,
        started_monotonic=time.monotonic(),
    )


async def _applied_revisions(request: Request) -> str | None:
    """The applied migration heads, sorted and comma-joined (one per Alembic branch).

    None means the database could not be read within the timeout; the reason is logged.
    """

    async def query() -> str:
        async with request.app.state.engine.connect() as connection:
            result = await connection.execute(
                text("SELECT version_num FROM public.alembic_version ORDER BY 1")
            )
            return ",".join(row[0] for row in result)

    try:
        return await asyncio.wait_for(query(), timeout=SCHEMA_QUERY_TIMEOUT_S)
    except TimeoutError:
        log.warning("healthz: schema query timed out after %.1f s", SCHEMA_QUERY_TIMEOUT_S)
    except Exception as exc:
        log.warning("healthz: could not read the schema version (%s)", type(exc).__name__)
    return None


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    build: BuildInfo = request.app.state.build_info
    return {
        "status": "ok",
        "version": build.version,
        "commit": build.commit,
        "commit_source": "process_start",
        "branch": build.branch,
        "schema_version": await _applied_revisions(request),
        "uptime_s": int(time.monotonic() - build.started_monotonic),
    }


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, str]:
    try:
        async with request.app.state.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "database unavailable"}
    return {"status": "ready"}
