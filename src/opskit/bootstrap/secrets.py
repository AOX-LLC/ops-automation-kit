"""Create the kit's generated secrets on first boot; never overwrite, never print.

Each consumer gets its own subdirectory on the kit-secrets volume, owned by its uid and
mounted only into that consumer. `make login` is the only way to read the human passwords.
"""

from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

import bcrypt

POSTGRES_UID = 999
N8N_UID = 1000
API_UID = 10001


@dataclass(frozen=True, slots=True)
class Consumer:
    directory: str
    uid: int


POSTGRES = Consumer("postgres", POSTGRES_UID)
N8N = Consumer("n8n", N8N_UID)
API = Consumer("api", API_UID)
MIGRATE = Consumer("migrate", API_UID)
LOGIN = Consumer("login", API_UID)
DB_ROLES = Consumer("dbroles", API_UID)

# secret name -> consumers that receive a copy
TOKENS: dict[str, tuple[Consumer, ...]] = {
    "postgres_superuser_password": (POSTGRES, DB_ROLES),
    "n8n_db_password": (POSTGRES, N8N),
    "opskit_owner_password": (POSTGRES, MIGRATE),
    "opskit_app_password": (POSTGRES, API),
    # The approver role is created by the db-roles step (not initdb), so existing databases
    # get it too; only the api's decision path and that step receive the password.
    "opskit_approver_password": (DB_ROLES, API),
    "n8n_encryption_key": (N8N,),
    "api_service_token": (API, N8N),
    "n8n_webhook_token": (N8N, LOGIN),
    "approver_session_secret": (API,),
}
# human passwords: plaintext only for `make login`, bcrypt hash for the service that checks it
PASSWORDS: dict[str, Consumer] = {
    "n8n_owner_password": N8N,
    "approver_password": API,
}


def _write_new(path: Path, value: str, uid: int) -> bool:
    """Write `value` to `path` only if the file does not exist yet. Returns True if written."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value)
    os.chown(path, uid, uid)
    return True


CONSUMERS = (POSTGRES, N8N, API, MIGRATE, LOGIN, DB_ROLES)


def _open_dirs(root: Path) -> None:
    for consumer in CONSUMERS:
        directory = root / consumer.directory
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)


def _seal_dirs(root: Path) -> None:
    """Hand each directory to its consumer, read-only."""
    for consumer in CONSUMERS:
        directory = root / consumer.directory
        directory.chmod(0o500)
        os.chown(directory, consumer.uid, consumer.uid)


def _shared_value(root: Path, name: str, consumers: tuple[Consumer, ...], make: str) -> str:
    """Reuse a value already written for any consumer, so copies never diverge."""
    for consumer in consumers:
        existing = root / consumer.directory / name
        if existing.exists():
            return existing.read_text(encoding="utf-8")
    return make


def ensure_secrets(root: Path) -> int:
    created = 0
    _open_dirs(root)

    for name, consumers in TOKENS.items():
        value = _shared_value(root, name, consumers, secrets.token_hex(32))
        for consumer in consumers:
            created += _write_new(root / consumer.directory / name, value, consumer.uid)

    for name, checker in PASSWORDS.items():
        plaintext = _shared_value(root, name, (LOGIN,), secrets.token_urlsafe(18))
        created += _write_new(root / LOGIN.directory / name, plaintext, LOGIN.uid)
        hashed = bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt()).decode()
        created += _write_new(root / checker.directory / f"{name}.bcrypt", hashed, checker.uid)
    _seal_dirs(root)
    return created


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/secrets")
    created = ensure_secrets(root)
    # Counts only: secret values never reach a log.
    print(f"kit-secrets: {created} file(s) created, existing files left unchanged")


if __name__ == "__main__":
    main()
