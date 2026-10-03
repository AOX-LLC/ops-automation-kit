"""Sending an approved draft: the only path from an approval to an outgoing reply.

n8n calls release after the approver page records "approved". Release spends the approval
(agent-core's consume: single use, bound to the payload hash) and returns the exact envelope
the approver saw, read from the stored draft, never from the caller. Sent and close only
record what happened; neither can send anything.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel

from opskit.api.auth import ServiceAuth
from opskit.core.errors import (
    ApprovalAlreadyResolvedError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ApprovalNotGrantedError,
    ApprovalPayloadMismatchError,
)
from opskit.core.ports import N8N_SERVICE, AuditEvent, Core
from opskit.db.engine import SessionFactory
from opskit.inbox import store
from opskit.receipts.store import session_factory_of

router = APIRouter(prefix="/v1/inbox/drafts", tags=["inbox"], dependencies=[ServiceAuth])

SEND_ACTION = "inbox.send_reply"


class Envelope(BaseModel):
    to: str
    subject: str
    in_reply_to: str
    body: str


def _core(request: Request) -> Core:
    core: Core = request.app.state.core
    return core


def _session_factory(request: Request) -> SessionFactory:
    session_factory = session_factory_of(request.app)
    if session_factory is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable")
    return session_factory


async def _draft_or_404(request: Request, draft_id: UUID) -> store.StoredDraft:
    draft = await store.get_draft(_session_factory(request), draft_id)
    if draft is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such draft")
    return draft


@router.post("/{draft_id}/release")
async def release(request: Request, draft_id: UUID) -> Envelope:
    draft = await _draft_or_404(request, draft_id)
    if draft.status != "pending" or draft.approval_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"draft is {draft.status}")
    # A hold that landed after the draft was made (two runs triaging at once) still wins.
    if await store.is_quarantined(_session_factory(request), draft.message_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "the message is held for review")
    try:
        await _core(request).approvals.consume(
            draft.approval_id,
            action=SEND_ACTION,
            payload=store.approval_payload(draft),
            principal=N8N_SERVICE,
        )
    except ApprovalNotFoundError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "the draft's approval is missing") from exc
    except ApprovalPayloadMismatchError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "the draft changed after it was approved; not sending"
        ) from exc
    except (ApprovalNotGrantedError, ApprovalAlreadyResolvedError, ApprovalExpiredError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "the draft is not approved to send") from exc
    session_factory = _session_factory(request)
    await store.set_draft_status(
        session_factory, draft_id, from_statuses=("pending",), to_status="approved"
    )
    return Envelope(**store.approval_payload(draft))


@router.post("/{draft_id}/sent", status_code=status.HTTP_204_NO_CONTENT)
async def mark_sent(request: Request, draft_id: UUID) -> Response:
    draft = await _draft_or_404(request, draft_id)
    session_factory = _session_factory(request)
    if not await store.set_draft_status(
        session_factory, draft_id, from_statuses=("approved",), to_status="sent", sent=True
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, f"draft is {draft.status}, not approved")
    await _core(request).audit.append(
        AuditEvent(
            action="inbox.reply_sent",
            actor_id=N8N_SERVICE.id,
            subject_id=str(draft_id),
            payload={"subject_type": "draft", "approval_id": str(draft.approval_id)},
        )
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{draft_id}/close", status_code=status.HTTP_204_NO_CONTENT)
async def close(request: Request, draft_id: UUID) -> Response:
    """Record that a draft will never be sent; the outcome comes from the approval itself.

    A rejected approval closes the draft as rejected. A pending approval past its expiry
    (the workflow's Wait timed out) is expired first, then the draft is closed as expired.
    An approved draft is released, not closed, and a still-live approval can't be closed.
    """
    draft = await _draft_or_404(request, draft_id)
    approvals = _core(request).approvals
    outcome: Literal["rejected", "expired"]
    if draft.approval_id is None:
        outcome = "rejected"
    else:
        approval = await approvals.get(draft.approval_id)
        if approval.status.value == "pending" and approval.is_expired(datetime.now(UTC)):
            approval = await approvals.expire(draft.approval_id)
        if approval.status.value == "rejected":
            outcome = "rejected"
        elif approval.status.value == "expired":
            outcome = "expired"
        else:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"the approval is {approval.status.value}; not closing"
            )
    session_factory = _session_factory(request)
    await store.set_draft_status(
        session_factory, draft_id, from_statuses=("draft", "pending"), to_status=outcome
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
