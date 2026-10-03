"""Approval requests from n8n. Deciding is not here: it lives on the approver page."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from opskit.api.auth import ServiceAuth
from opskit.approvals.resume import InvalidResumeUrl, internal_resume_target
from opskit.core.errors import ApprovalNotFoundError, NotFound
from opskit.core.ports import APPROVER_ROLE, N8N_SERVICE, ApprovalRequest, Core, run_uuid

router = APIRouter(prefix="/v1/approvals", tags=["approvals"], dependencies=[ServiceAuth])

MAX_EXPIRY_S = 7 * 24 * 3600


class RequestApproval(BaseModel):
    run_id: UUID
    # agent-core's action-name pattern: dotted lowercase, such as "inbox.reply".
    kind: str = Field(max_length=100, pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
    subject: dict[str, Any]
    summary: str | None = Field(default=None, min_length=1, max_length=500)
    resume_url: str = Field(max_length=512)
    expires_in_s: int = Field(gt=0, le=MAX_EXPIRY_S)


class ApprovalView(BaseModel):
    """What callers may see. The resume URL is deliberately absent."""

    approval_id: UUID
    run_id: UUID | None
    kind: str
    status: str
    requested_at: datetime
    expires_at: datetime
    decided_at: datetime | None

    @classmethod
    def of(cls, approval: ApprovalRequest) -> ApprovalView:
        run_context = approval.run_context
        return cls(
            approval_id=approval.id,
            run_id=run_uuid(run_context) if run_context is not None else None,
            kind=approval.action,
            status=approval.status.value,
            requested_at=approval.created_at,
            expires_at=approval.expires_at,
            decided_at=approval.resolved_at,
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
    approval = await core.approvals.submit(
        action=body.kind,
        summary=body.summary or f"Approve {body.kind}",
        payload=body.subject,
        requested_by=N8N_SERVICE,
        required_role=APPROVER_ROLE,
        ttl_seconds=body.expires_in_s,
        context=ctx,
        resume_url=body.resume_url,
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
    except ApprovalNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such approval") from exc


@router.post("/{approval_id}/expire")
async def expire_approval(request: Request, approval_id: UUID) -> ApprovalView:
    try:
        return ApprovalView.of(await _core(request).approvals.expire(approval_id))
    except ApprovalNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such approval") from exc
