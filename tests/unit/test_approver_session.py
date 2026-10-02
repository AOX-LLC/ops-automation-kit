import pytest

from opskit.api import approver_session
from opskit.api.approver_session import ApproverSession, LoginThrottle, SessionCodec


def test_session_round_trips_and_rejects_tampering() -> None:
    codec = SessionCodec("test-secret", max_age_s=60)
    session = ApproverSession.authenticated()
    cookie = codec.dumps(session)
    assert codec.loads(cookie) == session
    assert codec.loads(cookie[:-2] + "xx") is None
    assert SessionCodec("other-secret", max_age_s=60).loads(cookie) is None
    assert codec.loads(None) is None


def test_csrf_comparison() -> None:
    session = ApproverSession.anonymous()
    assert session.csrf_matches(session.csrf_token)
    assert not session.csrf_matches("")
    assert not session.csrf_matches(session.csrf_token + "x")


def test_login_issues_a_fresh_csrf_token() -> None:
    assert ApproverSession.anonymous().csrf_token != ApproverSession.authenticated().csrf_token


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
