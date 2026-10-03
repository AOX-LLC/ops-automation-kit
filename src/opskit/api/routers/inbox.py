"""Inbox for n8n: list new mail, triage it, draft replies and request their approval."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from opskit.api.auth import ServiceAuth
from opskit.approvals.resume import InvalidResumeUrl, internal_resume_target
from opskit.config import Settings
from opskit.core.errors import ModelRefusalError, NotFound, ReplayMissError, StructuredOutputError
from opskit.core.ports import APPROVER_ROLE, N8N_SERVICE, Core, RunContext
from opskit.db.engine import SessionFactory
from opskit.inbox import store
from opskit.inbox.mail import InboundMessage, MailpitSource
from opskit.inbox.service import (
    DraftOutcome,
    NotDraftable,
    TriageOutcome,
    draft_reply,
    load_profile,
    triage_message,
)
from opskit.receipts.store import session_factory_of

router = APIRouter(prefix="/v1/inbox", tags=["inbox"], dependencies=[ServiceAuth])

MAILPIT_TIMEOUT_S = 5.0
APPROVAL_TTL_S = 72 * 3600
SUMMARY_MAX_CHARS = 500

Limit = Annotated[int, Query(ge=1, le=100)]


class PendingItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    message_id: str
    mailpit_id: str
    from_: str = Field(serialization_alias="from")
    subject: str
    received_at: str | None


class PendingPage(BaseModel):
    items: list[PendingItem]
    next_cursor: str | None = None


class MessageRequest(BaseModel):
    run_id: UUID
    message_id: str = Field(min_length=1, max_length=998)


class DraftCreated(DraftOutcome):
    draft_id: UUID


class ApprovalRequestBody(BaseModel):
    run_id: UUID
    resume_url: str = Field(max_length=512)


class ApprovalCreated(BaseModel):
    approval_id: UUID
    draft_id: UUID


def _database(request: Request) -> SessionFactory:
    factory = session_factory_of(request.app)
    if factory is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable")
    return factory


def _core(request: Request) -> Core:
    core: Core = request.app.state.core
    return core


async def _run_context(request: Request, run_id: UUID) -> RunContext:
    try:
        return await _core(request).runs.get(run_id)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found") from exc


@asynccontextmanager
async def _http_client(request: Request) -> AsyncIterator[httpx.AsyncClient]:
    shared: httpx.AsyncClient | None = getattr(request.app.state, "http_client", None)
    if shared is not None:
        yield shared
        return
    async with httpx.AsyncClient(timeout=MAILPIT_TIMEOUT_S) as client:
        yield client


def _replay_miss(exc: ReplayMissError) -> HTTPException:
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE, f"no recording for replay key {exc.key or 'unknown'}"
    )


@router.get("/pending", response_model_by_alias=True)
async def pending(request: Request, limit: Limit = 50) -> PendingPage:
    """New messages that are not triaged yet; each is stored so triage can read it."""
    settings: Settings = request.app.state.settings
    factory = _database(request)
    done = await store.triaged_ids(factory)
    items: list[PendingItem] = []
    try:
        async with _http_client(request) as client:
            source = MailpitSource(settings.mailpit_api_url, client)
            for ref in await source.list_new(known=done, limit=limit):
                msg = await source.fetch(ref)
                await store.save_message(factory, msg)
                items.append(
                    PendingItem(
                        message_id=msg.message_id,
                        mailpit_id=msg.mailpit_id,
                        from_=msg.from_header,
                        subject=msg.subject,
                        received_at=msg.received_at.isoformat() if msg.received_at else None,
                    )
                )
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "mailpit unavailable") from exc
    return PendingPage(items=items)


async def _stored_message(factory: SessionFactory, message_id: str) -> InboundMessage:
    msg = await store.load_message(factory, message_id)
    if msg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown message")
    return msg


@router.post("/triage")
async def triage(request: Request, body: MessageRequest) -> TriageOutcome:
    factory = _database(request)
    msg = await _stored_message(factory, body.message_id)
    ctx = await _run_context(request, body.run_id)
    existing = await store.load_triage(factory, body.message_id)
    if existing is not None:
        return existing
    try:
        outcome = await triage_message(_core(request).models, ctx, msg)
    except ReplayMissError as exc:
        raise _replay_miss(exc) from exc
    except (ModelRefusalError, StructuredOutputError) as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"triage failed: {type(exc).__name__}"
        ) from exc
    await store.save_triage(factory, body.run_id, outcome)
    return outcome


@router.post("/drafts", status_code=status.HTTP_201_CREATED)
async def create_draft(request: Request, body: MessageRequest) -> DraftCreated:
    settings: Settings = request.app.state.settings
    factory = _database(request)
    msg = await _stored_message(factory, body.message_id)
    ctx = await _run_context(request, body.run_id)
    triaged = await store.load_triage(factory, body.message_id)
    if triaged is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "message is not triaged yet")
    if await store.get_draft_for_message(factory, body.message_id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "a draft already exists for this message")
    try:
        outcome = await draft_reply(
            _core(request).models, ctx, msg, triaged, load_profile(settings)
        )
    except NotDraftable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ReplayMissError as exc:
        raise _replay_miss(exc) from exc
    except OSError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "business profile missing"
        ) from exc
    try:
        draft_id = await store.save_draft(factory, body.run_id, body.message_id, outcome)
    except store.DraftConflict as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "a draft already exists for this message"
        ) from exc
    return DraftCreated(draft_id=draft_id, **outcome.model_dump())


@router.post("/drafts/{draft_id}/approval", status_code=status.HTTP_201_CREATED)
async def request_draft_approval(
    request: Request, draft_id: UUID, body: ApprovalRequestBody
) -> ApprovalCreated:
    """Ask for a human decision on the draft, exactly as stored; it moves draft -> pending."""
    settings: Settings = request.app.state.settings
    try:
        internal_resume_target(body.resume_url, settings)
    except InvalidResumeUrl as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    factory = _database(request)
    draft = await store.get_draft(factory, draft_id)
    if draft is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown draft")
    if draft.status != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, f"draft is {draft.status}, not awaiting")
    ctx = await _run_context(request, body.run_id)
    core = _core(request)
    approval = await core.approvals.submit(
        action="inbox.send_reply",
        summary=f"Reply to {draft.to}: {draft.subject}"[:SUMMARY_MAX_CHARS],
        payload=store.approval_payload(draft),
        requested_by=N8N_SERVICE,
        required_role=APPROVER_ROLE,
        ttl_seconds=APPROVAL_TTL_S,
        context=ctx,
        resume_url=body.resume_url,
    )
    moved = await store.set_draft_status(
        factory,
        draft_id,
        from_statuses=("draft",),
        to_status="pending",
        approval_id=approval.id,
    )
    if not moved:
        # Another request won the race; withdraw this approval so only one stays open.
        await core.approvals.expire(approval.id)
        raise HTTPException(status.HTTP_409_CONFLICT, "draft is no longer awaiting approval")
    return ApprovalCreated(approval_id=approval.id, draft_id=draft_id)


@router.get("/summary", response_model_by_alias=True)
async def run_summary(request: Request, run_id: UUID) -> store.InboxSummary:
    return await store.summary(_database(request), run_id)
