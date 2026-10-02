"""Approver login session: a signed cookie carrying a CSRF token and the login flag.

The cookie is scoped to /approver, HttpOnly and SameSite=Strict. Every POST must echo the
session's CSRF token in a form field. Logging in issues a fresh session (new token).
"""

from __future__ import annotations

import hmac
import secrets
import time
from collections import deque
from dataclasses import dataclass

from itsdangerous import BadSignature, URLSafeTimedSerializer

COOKIE_NAME = "kit_approver"
COOKIE_PATH = "/approver"
MAX_FAILURES = 5
FAILURE_WINDOW_S = 300
LOCKOUT_S = 300


@dataclass(frozen=True, slots=True)
class ApproverSession:
    csrf_token: str
    logged_in: bool

    @classmethod
    def anonymous(cls) -> ApproverSession:
        return cls(csrf_token=secrets.token_urlsafe(32), logged_in=False)

    @classmethod
    def authenticated(cls) -> ApproverSession:
        return cls(csrf_token=secrets.token_urlsafe(32), logged_in=True)

    def csrf_matches(self, supplied: str) -> bool:
        return hmac.compare_digest(self.csrf_token.encode(), supplied.encode())


class SessionCodec:
    def __init__(self, secret: str, max_age_s: int) -> None:
        self._serializer = URLSafeTimedSerializer(secret, salt="approver-session")
        self.max_age_s = max_age_s

    def dumps(self, session: ApproverSession) -> str:
        return self._serializer.dumps({"csrf": session.csrf_token, "auth": session.logged_in})

    def loads(self, cookie: str | None) -> ApproverSession | None:
        if not cookie:
            return None
        try:
            data = self._serializer.loads(cookie, max_age=self.max_age_s)
        except BadSignature:
            return None
        return ApproverSession(csrf_token=str(data["csrf"]), logged_in=bool(data["auth"]))


class LoginThrottle:
    """Locks the single approver login after repeated failures (one process, localhost only)."""

    def __init__(self) -> None:
        self._failures: deque[float] = deque()
        self._locked_until = 0.0

    def is_locked(self) -> bool:
        return time.monotonic() < self._locked_until

    def record_failure(self) -> None:
        now = time.monotonic()
        self._failures.append(now)
        while self._failures and now - self._failures[0] > FAILURE_WINDOW_S:
            self._failures.popleft()
        if len(self._failures) >= MAX_FAILURES:
            self._locked_until = now + LOCKOUT_S
            self._failures.clear()

    def record_success(self) -> None:
        self._failures.clear()
