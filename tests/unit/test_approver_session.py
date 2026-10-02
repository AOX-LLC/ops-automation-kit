from uuid import uuid4

import pytest

from opskit.api import approver_session
from opskit.api.approver_session import ApproverSession, LoginThrottle, SessionCodec


def test_logged_in_cookie_carries_only_the_session_id() -> None:
    codec = SessionCodec("test-secret", max_age_s=60)
    session = ApproverSession(csrf_token="token-stays-server-side", session_id=uuid4())
    cookie = codec.dumps(session)
    assert codec.loads(cookie) == {"sid": str(session.session_id)}
    assert "token-stays-server-side" not in str(codec.loads(cookie))


def test_cookie_rejects_tampering_and_other_keys() -> None:
    codec = SessionCodec("test-secret", max_age_s=60)
    cookie = codec.dumps(ApproverSession.anonymous())
    assert codec.loads(cookie) is not None
    assert codec.loads(cookie[:-2] + "xx") is None
    assert SessionCodec("other-secret", max_age_s=60).loads(cookie) is None
    assert codec.loads(None) is None


def test_csrf_comparison() -> None:
    session = ApproverSession.anonymous()
    assert session.csrf_matches(session.csrf_token)
    assert not session.csrf_matches("")
    assert not session.csrf_matches(session.csrf_token + "x")


def test_anonymous_sessions_are_not_logged_in() -> None:
    assert not ApproverSession.anonymous().logged_in
    assert ApproverSession(csrf_token="x", session_id=uuid4()).logged_in


def test_throttle_locks_after_five_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(approver_session.time, "monotonic", lambda: clock[0])
    throttle = LoginThrottle()
    for _ in range(4):
        throttle.record_failure()
    assert not throttle.is_locked()
    throttle.record_failure()
    assert throttle.is_locked()
    clock[0] += approver_session.LOCKOUT_S + 1
    assert not throttle.is_locked()


def test_old_failures_fall_out_of_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(approver_session.time, "monotonic", lambda: clock[0])
    throttle = LoginThrottle()
    for _ in range(4):
        throttle.record_failure()
    clock[0] += approver_session.FAILURE_WINDOW_S + 1
    throttle.record_failure()
    assert not throttle.is_locked()
