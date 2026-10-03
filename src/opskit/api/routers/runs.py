"""Run records, one per n8n execution."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from opskit.api.auth import ServiceAuth
from opskit.core.errors import NotFound
from opskit.core.ports import Core

router = APIRouter(prefix="/v1/runs", tags=["runs"], dependencies=[ServiceAuth])

Workflow = Literal["kit_smoke", "receipts", "leads", "inbox"]


N8N_ID_PATTERN = r"^([A-Za-z0-9][A-Za-z0-9._:/-]{0,63})?$"


class StartRun(BaseModel):
    workflow: Workflow
    # The form core_0010 enforces (and agent-core's id pattern): a 422 here, not a 500 there.
    n8n_workflow_id: str | None = Field(default=None, max_length=64, pattern=N8N_ID_PATTERN)
    n8n_execution_id: str | None = Field(default=None, max_length=64, pattern=N8N_ID_PATTERN)


class RunStarted(BaseModel):
    run_id: UUID
    mode: str


class FinishRun(BaseModel):
    status: Literal["succeeded", "failed"]


@router.post("", status_code=status.HTTP_201_CREATED)
async def start_run(request: Request, body: StartRun) -> RunStarted:
    core: Core = request.app.state.core
    ctx = await core.runs.start(
        workflow=body.workflow,
        n8n_workflow_id=body.n8n_workflow_id,
        n8n_execution_id=body.n8n_execution_id,
    )
    return RunStarted(run_id=UUID(ctx.run_id), mode=core.mode.value)


@router.post("/{run_id}/finish", status_code=status.HTTP_204_NO_CONTENT)
async def finish_run(request: Request, run_id: UUID, body: FinishRun) -> None:
    core: Core = request.app.state.core
    try:
        await core.runs.finish(run_id, succeeded=body.status == "succeeded")
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such run") from exc
