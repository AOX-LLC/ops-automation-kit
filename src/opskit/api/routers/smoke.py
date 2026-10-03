"""One mock model call through the adapter, so the smoke workflow exercises the whole path."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from opskit.api.auth import ServiceAuth
from opskit.core.errors import NotFound, ReplayMissError
from opskit.core.ports import Core, PromptRef, Tier

router = APIRouter(prefix="/v1/smoke", tags=["smoke"], dependencies=[ServiceAuth])

CLASSIFY_PROMPT = PromptRef(
    id="smoke.classify",
    version=1,
    system="You label short messages. Answer only with the requested structure.",
    template=(
        "Label this message as 'wiring_check' or 'other' and say why in one sentence.\n\n${text}"
    ),
)


class ClassifyRequest(BaseModel):
    run_id: UUID
    text: str = Field(min_length=1, max_length=2000)


class SmokeLabel(BaseModel):
    label: Literal["wiring_check", "other"]
    reason: str


class ClassifyResponse(BaseModel):
    label: str
    reason: str
    fixture_key: str
    mode: str


@router.post("/classify")
async def classify(request: Request, body: ClassifyRequest) -> ClassifyResponse:
    core: Core = request.app.state.core
    try:
        ctx = await core.runs.get(body.run_id)
        result = await core.models.call(
            CLASSIFY_PROMPT,
            inputs={"text": body.text},
            output=SmokeLabel,
            tier=Tier.SMALL,
            context=ctx,
        )
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such run") from exc
    except ReplayMissError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"no recording for replay key {exc.key}"
        ) from exc
    return ClassifyResponse(
        label=result.output.label,
        reason=result.output.reason,
        fixture_key=result.replay_key,
        mode=result.mode.value,
    )
