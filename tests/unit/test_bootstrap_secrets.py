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


def test_the_approver_password_reaches_only_the_api_and_the_db_roles_step(tmp_path) -> None:  # type: ignore[no-untyped-def]
    kit_secrets.ensure_secrets(tmp_path)
    name = "opskit_approver_password"
    holders = {path.parent.name for path in tmp_path.rglob(name)}
    assert holders == {"api", "dbroles"}
    assert (tmp_path / "api" / name).read_text() == (tmp_path / "dbroles" / name).read_text()
    assert (tmp_path / "api" / name).read_text() != (
        tmp_path / "api" / "opskit_app_password"
    ).read_text()
    # n8n and the migration step never see it.
    assert not (tmp_path / "n8n" / name).exists()
    assert not (tmp_path / "migrate" / name).exists()


def test_an_existing_volume_gains_the_approver_password_and_keeps_the_rest(tmp_path) -> None:  # type: ignore[no-untyped-def]
    kit_secrets.ensure_secrets(tmp_path)
    for path in tmp_path.rglob("opskit_approver_password"):
        path.parent.chmod(0o700)
        path.unlink()
    (tmp_path / "dbroles" / "postgres_superuser_password").parent.chmod(0o700)
    before = snapshot(tmp_path)
    assert kit_secrets.ensure_secrets(tmp_path) == 2
    after = snapshot(tmp_path)
    assert {k: v for k, v in after.items() if "approver_password" not in k} == {
        k: v for k, v in before.items() if "approver_password" not in k
    }
