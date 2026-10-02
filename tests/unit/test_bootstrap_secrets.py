import os

import bcrypt
import pytest

from opskit.bootstrap import secrets as kit_secrets


@pytest.fixture(autouse=True)
def no_chown(monkeypatch: pytest.MonkeyPatch) -> None:
    # Tests run unprivileged; ownership is exercised by the compose smoke test.
    monkeypatch.setattr(kit_secrets.os, "chown", lambda *args: None)


def snapshot(root):  # type: ignore[no-untyped-def]
    return {
        str(path.relative_to(root)): (path.read_text(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_first_run_creates_everything(tmp_path) -> None:  # type: ignore[no-untyped-def]
    created = kit_secrets.ensure_secrets(tmp_path)
    assert created > 0
    assert (tmp_path / "postgres" / "opskit_app_password").read_text() == (
        tmp_path / "api" / "opskit_app_password"
    ).read_text()
    password = (tmp_path / "login" / "approver_password").read_text()
    hashed = (tmp_path / "api" / "approver_password.bcrypt").read_text()
    assert bcrypt.checkpw(password.encode(), hashed.encode())
    assert not (tmp_path / "api" / "approver_password").exists()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert oct(os.stat(path).st_mode & 0o777) == "0o400"


def test_second_run_changes_nothing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    kit_secrets.ensure_secrets(tmp_path)
    before = snapshot(tmp_path)
    assert kit_secrets.ensure_secrets(tmp_path) == 0
    assert snapshot(tmp_path) == before
