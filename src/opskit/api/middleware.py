"""Security headers and a request-size cap for every response."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware

MAX_BODY_BYTES = 1_048_576
PAGE_CSP = "default-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"

type CallNext = Callable[[Request], Awaitable[Response]]


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return PlainTextResponse("request body too large", status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = PAGE_CSP
        if request.url.path.startswith(("/v1/", "/approver")):
            response.headers["Cache-Control"] = "no-store"
        return response
