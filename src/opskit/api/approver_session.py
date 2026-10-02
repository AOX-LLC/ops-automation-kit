"""Approver login sessions.

Before login, a signed cookie carries only a CSRF token for the login form. Logging in creates
a server-side session row (new id, new CSRF token, absolute expiry); the cookie then carries
only the signed session id. Logout revokes the row, so a copied cookie stops working at once.
The cookie is scoped to /approver, HttpOnly and SameSite=Strict.
"""

from __future__ import annotations

import hmac
import secrets
import time
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import func, insert, select, update

from opskit.db.engine import SessionFactory
from opskit.db.tables import approver_sessions

COOKIE_NAME = "kit_approver"
COOKIE_PATH = "/approver"
MAX_FAILURES = 5
FAILURE_WINDOW_S = 300
LOCKOUT_S = 300


@dataclass(frozen=True, slots=True)
class ApproverSession:
    csrf_token: str
    session_id: UUID | None = None

    @property
    def logged_in(self) -> bool:
        return self.session_id is not None

    @classmethod
    def anonymous(cls) -> ApproverSession:
        return cls(csrf_token=secrets.token_urlsafe(32))

    def csrf_matches(self, supplied: str) -> bool:
        return hmac.compare_digest(self.csrf_token.encode(), supplied.encode())


class SessionCodec:
    def __init__(self, secret: str, max_age_s: int) -> None:
        self._serializer = URLSafeTimedSerializer(secret, salt="approver-session")
        self.max_age_s = max_age_s

    def dumps(self, session: ApproverSession) -> str:
        if session.session_id is not None:
            return self._serializer.dumps({"sid": str(session.session_id)})
        return self._serializer.dumps({"csrf": session.csrf_token})

    def loads(self, cookie: str | None) -> dict[str, str] | None:
        """The verified cookie payload: {"sid": ...} after login, {"csrf": ...} before."""
        if not cookie:
            return None
        try:
            data = self._serializer.loads(cookie, max_age=self.max_age_s)
        except BadSignature:
            return None
        return data if isinstance(data, dict) else None


class SessionStore:
    """Server-side approver sessions: created on login, checked on every request, revocable."""

    def __init__(self, session_factory: SessionFactory, lifetime: timedelta) -> None:
        self._session_factory = session_factory
        self._lifetime = lifetime

    async def create(self) -> ApproverSession:
        csrf_token = secrets.token_urlsafe(32)
        statement = (
            insert(approver_sessions)
            .values(csrf_token=csrf_token, expires_at=func.now() + self._lifetime)
            .returning(approver_sessions.c.id)
        )
        async with self._session_factory.begin() as db:
            session_id = (await db.execute(statement)).scalar_one()
        return ApproverSession(csrf_token=csrf_token, session_id=session_id)

    async def load(self, session_id: UUID) -> ApproverSession | None:
        """The live session with this id, or None if unknown, revoked or past its expiry."""
        query = select(approver_sessions.c.csrf_token).where(
            approver_sessions.c.id == session_id,
            approver_sessions.c.revoked_at.is_(None),
            approver_sessions.c.expires_at > func.now(),
        )
        async with self._session_factory() as db:
            csrf_token = (await db.execute(query)).scalar_one_or_none()
        return None if csrf_token is None else ApproverSession(csrf_token, session_id)

    async def revoke(self, session_id: UUID) -> None:
        statement = (
            update(approver_sessions)
            .where(approver_sessions.c.id == session_id, approver_sessions.c.revoked_at.is_(None))
            .values(revoked_at=func.now())
        )
        async with self._session_factory.begin() as db:
            await db.execute(statement)


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
