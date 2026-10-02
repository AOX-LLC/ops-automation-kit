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
from opskit.core.errors import ApprovalExpired, ApprovalNotPending, NotFound
from opskit.core.ports import Core, Decision

router = APIRouter(prefix="/approver", include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parents[1] / "templates")
ACTOR = "approver"
BCRYPT_MAX_BYTES = 72


def _refuse_bearer(request: Request) -> None:
    if "authorization" in request.headers:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "the approver page takes no API tokens")


def _session(request: Request) -> ApproverSession | None:
    _refuse_bearer(request)
    session: ApproverSession | None = request.app.state.session_codec.loads(
        request.cookies.get(COOKIE_NAME)
    )
    return session


def _require_login(request: Request) -> ApproverSession:
    session = _session(request)
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


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> Response:
    session = _session(request) or ApproverSession.anonymous()
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
    session = _require_csrf(_session(request), csrf_token)
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
            ctx=None,
            actor=ACTOR,
            action="approver.login",
            subject_type=None,
            subject_id=None,
            details={"outcome": "failure"},
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
        ctx=None,
        actor=ACTOR,
        action="approver.login",
        subject_type=None,
        subject_id=None,
        details={"outcome": "success"},
    )
    redirect = RedirectResponse("/approver/", status_code=status.HTTP_303_SEE_OTHER)
    return _with_cookie(request, redirect, ApproverSession.authenticated())


@router.post("/logout")
async def logout(
    request: Request, csrf_token: Annotated[str, Form(max_length=128)] = ""
) -> Response:
    _require_csrf(_require_login(request), csrf_token)
    redirect = RedirectResponse("/approver/login", status_code=status.HTTP_303_SEE_OTHER)
    redirect.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
    return redirect


@router.get("/", response_class=HTMLResponse)
async def queue(request: Request, cursor: str | None = None) -> Response:
    session = _require_login(request)
    page = await _core(request).approvals.list_pending(limit=25, cursor=cursor)
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
    session = _require_login(request)
    try:
        approval = await _core(request).approvals.get(approval_id)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such approval") from exc
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "csrf_token": session.csrf_token,
            "approval": approval,
            "subject_json": json.dumps(approval.subject, indent=2, sort_keys=True),
            "error": None,
        },
    )


@router.post("/approvals/{approval_id}/decision")
async def decide(
    request: Request,
    approval_id: UUID,
    decision: Annotated[Decision, Form()],
    csrf_token: Annotated[str, Form(max_length=128)] = "",
    note: Annotated[str, Form(max_length=2000)] = "",
) -> Response:
    session = _require_csrf(_require_login(request), csrf_token)
    approvals = _core(request).approvals
    try:
        await approvals.decide(approval_id, decision=decision, actor=ACTOR, note=note or None)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such approval") from exc
    except (ApprovalExpired, ApprovalNotPending) as exc:
        approval = await approvals.get(approval_id)
        message = (
            "This approval expired before a decision was made."
            if isinstance(exc, ApprovalExpired)
            else f"This approval was already {approval.status.value}."
        )
        return templates.TemplateResponse(
            request,
            "detail.html",
            {
                "csrf_token": session.csrf_token,
                "approval": approval,
                "subject_json": json.dumps(approval.subject, indent=2, sort_keys=True),
                "error": message,
            },
            status_code=status.HTTP_409_CONFLICT,
        )
    return RedirectResponse("/approver/", status_code=status.HTTP_303_SEE_OTHER)
