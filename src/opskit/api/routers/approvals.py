"""Approval requests from n8n. Deciding is not here: it lives on the approver page."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from opskit.api.auth import ServiceAuth
from opskit.approvals.resume import InvalidResumeUrl, internal_resume_target
from opskit.core.errors import NotFound
from opskit.core.ports import Approval, Core

router = APIRouter(prefix="/v1/approvals", tags=["approvals"], dependencies=[ServiceAuth])

MAX_EXPIRY_S = 7 * 24 * 3600


class RequestApproval(BaseModel):
    run_id: UUID
    kind: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_.]+$")
    subject: dict[str, Any]
    resume_url: str = Field(max_length=512)
    expires_in_s: int = Field(gt=0, le=MAX_EXPIRY_S)


class ApprovalView(BaseModel):
    """What callers may see. The resume URL is deliberately absent."""

    approval_id: UUID
    run_id: UUID
    kind: str
    status: str
    requested_at: datetime
    expires_at: datetime
    decided_at: datetime | None

    @classmethod
    def of(cls, approval: Approval) -> ApprovalView:
        return cls(
            approval_id=approval.id,
            run_id=approval.run_id,
            kind=approval.kind,
            status=approval.status.value,
            requested_at=approval.requested_at,
            expires_at=approval.expires_at,
            decided_at=approval.decided_at,
        )


def _core(request: Request) -> Core:
    core: Core = request.app.state.core
    return core


@router.post("", status_code=status.HTTP_201_CREATED)
async def request_approval(request: Request, body: RequestApproval) -> ApprovalView:
    try:
        internal_resume_target(body.resume_url, request.app.state.settings)
    except InvalidResumeUrl as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    core = _core(request)
    try:
        ctx = await core.runs.get(body.run_id)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such run") from exc
    approval = await core.approvals.request(
        ctx=ctx,
        kind=body.kind,
        subject=body.subject,
        resume_url=body.resume_url,
        expires_in=timedelta(seconds=body.expires_in_s),
    )
    return ApprovalView.of(approval)


@router.get("/{approval_id}")
async def get_approval(request: Request, approval_id: UUID) -> ApprovalView:
    """What a workflow checks after resuming: the decision as the helper recorded it.

    The resume POST body is not proof of approval (anyone holding the signed URL can send
    one), so side effects branch on this status instead.
    """
    try:
        return ApprovalView.of(await _core(request).approvals.get(approval_id))
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such approval") from exc


@router.post("/{approval_id}/expire")
async def expire_approval(request: Request, approval_id: UUID) -> ApprovalView:
    try:
        return ApprovalView.of(await _core(request).approvals.expire(approval_id))
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such approval") from exc
