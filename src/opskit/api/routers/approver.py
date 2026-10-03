"""The approver page: a small server-rendered queue where a human approves or rejects.

Only the approver session cookie opens it. A bearer token (what n8n holds) is refused
outright, and no `/v1` route can decide an approval.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated
from uuid import UUID

import bcrypt
from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from opskit.api.approver_session import COOKIE_NAME, COOKIE_PATH, ApproverSession
from opskit.core.errors import (
    ApprovalAlreadyResolvedError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
)
from opskit.core.ports import APPROVER, ApprovalRequest, AuditEvent, Core, Decision
from opskit.inbox.view import ACTION as INBOX_REPLY_ACTION
from opskit.inbox.view import InboxReplyView, load_inbox_reply_view
from opskit.receipts.store import session_factory_of

router = APIRouter(prefix="/approver", include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parents[1] / "templates")
BCRYPT_MAX_BYTES = 72
# agent-core stores the decision note as ApprovalRequest.reason, at most 500 characters.
NOTE_MAX_CHARS = 500


def _refuse_bearer(request: Request) -> None:
    if "authorization" in request.headers:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "the approver page takes no API tokens")


async def _session(request: Request) -> ApproverSession | None:
    """The caller's session: a live server-side one after login, the login-form one before."""
    _refuse_bearer(request)
    payload = request.app.state.session_codec.loads(request.cookies.get(COOKIE_NAME))
    if payload is None:
        return None
    if "sid" in payload:
        try:
            session_id = UUID(payload["sid"])
        except ValueError:
            return None
        live: ApproverSession | None = await request.app.state.session_store.load(session_id)
        return live
    return ApproverSession(csrf_token=str(payload.get("csrf", ""))) if payload.get("csrf") else None


async def _require_login(request: Request) -> ApproverSession:
    session = await _session(request)
    if session is None or not session.logged_in:
        raise HTTPException(status.HTTP_303_SEE_OTHER, headers={"Location": "/approver/login"})
    return session


def _require_csrf(session: ApproverSession | None, supplied: str) -> ApproverSession:
    if session is None or not session.csrf_matches(supplied):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing or invalid CSRF token")
    return session


def _with_cookie(request: Request, response: Response, session: ApproverSession) -> Response:
    codec = request.app.state.session_codec
    response.set_cookie(
        COOKIE_NAME,
        codec.dumps(session),
        max_age=codec.max_age_s,
        path=COOKIE_PATH,
        httponly=True,
        samesite="strict",
        secure=False,  # plain HTTP on 127.0.0.1 only; see README
    )
    return response


def _core(request: Request) -> Core:
    core: Core = request.app.state.core
    return core


async def _inbox_view(
    request: Request, approval: ApprovalRequest, payload: dict[str, object]
) -> InboxReplyView | None:
    """The dedicated reply view for an inbox draft; other actions keep the generic JSON."""
    factory = session_factory_of(request.app)
    if approval.action != INBOX_REPLY_ACTION or factory is None:
        return None
    return await load_inbox_reply_view(factory, payload)


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> Response:
    session = await _session(request)
    if session is not None and session.logged_in:
        return RedirectResponse("/approver/", status_code=status.HTTP_303_SEE_OTHER)
    session = session or ApproverSession.anonymous()
    page = templates.TemplateResponse(
        request, "login.html", {"csrf_token": session.csrf_token, "error": None}
    )
    return _with_cookie(request, page, session)


@router.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    password: Annotated[str, Form(max_length=256)],
    csrf_token: Annotated[str, Form(max_length=128)] = "",
) -> Response:
    session = _require_csrf(await _session(request), csrf_token)
    throttle = request.app.state.login_throttle
    audit = _core(request).audit
    if throttle.is_locked():
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts; try later")

    expected_hash: bytes = request.app.state.approver_password_hash
    candidate = password.encode()
    # bcrypt rejects inputs over 72 bytes; such a password can never match, so it is a failure.
    if len(candidate) > BCRYPT_MAX_BYTES or not bcrypt.checkpw(candidate, expected_hash):
        throttle.record_failure()
        await audit.append(
            AuditEvent(
                action="approver.login", actor_id=APPROVER.id, payload={"outcome": "failure"}
            )
        )
        page = templates.TemplateResponse(
            request,
            "login.html",
            {"csrf_token": session.csrf_token, "error": "That password is not right."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
        return _with_cookie(request, page, session)

    throttle.record_success()
    await audit.append(
        AuditEvent(action="approver.login", actor_id=APPROVER.id, payload={"outcome": "success"})
    )
    redirect = RedirectResponse("/approver/", status_code=status.HTTP_303_SEE_OTHER)
    return _with_cookie(request, redirect, await request.app.state.session_store.create())


@router.post("/logout")
async def logout(
    request: Request, csrf_token: Annotated[str, Form(max_length=128)] = ""
) -> Response:
    session = _require_csrf(await _require_login(request), csrf_token)
    if session.session_id is not None:
        await request.app.state.session_store.revoke(session.session_id)
        await _core(request).audit.append(
            AuditEvent(action="approver.logout", actor_id=APPROVER.id, payload={})
        )
    redirect = RedirectResponse("/approver/login", status_code=status.HTTP_303_SEE_OTHER)
    redirect.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
    return redirect


@router.get("/", response_class=HTMLResponse)
async def queue(request: Request, cursor: str | None = None) -> Response:
    session = await _require_login(request)
    page = await _core(request).approvals.list_pending_page(APPROVER, limit=25, cursor=cursor)
    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "csrf_token": session.csrf_token,
            "approvals": page.items,
            "next_cursor": page.next_cursor,
        },
    )


@router.get("/approvals/{approval_id}", response_class=HTMLResponse)
async def detail(request: Request, approval_id: UUID) -> Response:
    session = await _require_login(request)
    try:
        approval = await _core(request).approvals.get(approval_id)
        payload = await _core(request).approvals.payload_of(approval_id)
    except ApprovalNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such approval") from exc
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "csrf_token": session.csrf_token,
            "approval": approval,
            "subject_json": json.dumps(payload, indent=2, sort_keys=True),
            "inbox_view": await _inbox_view(request, approval, payload),
            "error": None,
        },
    )


@router.post("/approvals/{approval_id}/decision")
async def decide(
    request: Request,
    approval_id: UUID,
    decision: Annotated[Decision, Form()],
    csrf_token: Annotated[str, Form(max_length=128)] = "",
    note: Annotated[str, Form(max_length=NOTE_MAX_CHARS)] = "",
) -> Response:
    session = _require_csrf(await _require_login(request), csrf_token)
    approvals = _core(request).approvals
    try:
        await approvals.resolve(
            approval_id, decision=decision, principal=APPROVER, reason=note or None
        )
    except ApprovalNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such approval") from exc
    except (ApprovalExpiredError, ApprovalAlreadyResolvedError) as exc:
        approval = await approvals.get(approval_id)
        payload = await approvals.payload_of(approval_id)
        message = (
            "This approval expired before a decision was made."
            if isinstance(exc, ApprovalExpiredError)
            else f"This approval was already {approval.status.value}."
        )
        return templates.TemplateResponse(
            request,
            "detail.html",
            {
                "csrf_token": session.csrf_token,
                "approval": approval,
                "subject_json": json.dumps(payload, indent=2, sort_keys=True),
                "inbox_view": await _inbox_view(request, approval, payload),
                "error": message,
            },
            status_code=status.HTTP_409_CONFLICT,
        )
    return RedirectResponse("/approver/", status_code=status.HTTP_303_SEE_OTHER)
