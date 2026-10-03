"""Create or refresh the approver database role; runs before migrations on every boot.

`opskit_approver` is the only role that may move an approval to approved or rejected (a
database trigger enforces it). It is made here rather than by the Postgres init script so a
database created before the role split gets it too: init scripts run only on an empty data
directory. Idempotent. The password comes from a secret file and never reaches a log.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg import sql

APPROVER_ROLE = "opskit_approver"
DATABASE = "opskit"


def ensure_approver_role(connection: psycopg.Connection[object], password: str) -> bool:
    """Create the role if missing, set its password, and let it connect. True if created."""
    role = sql.Identifier(APPROVER_ROLE)
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (APPROVER_ROLE,))
        created = cursor.fetchone() is None
        if created:
            cursor.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
                    "NOBYPASSRLS"
                ).format(role)
            )
        # Every boot: the attributes, whatever they were, and a refusal if the role has grown
        # any membership (it must not inherit another role's rights).
        cursor.execute(
            sql.SQL(
                "ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            ).format(role)
        )
        cursor.execute(
            "SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
            "WHERE r.rolname = %s",
            (APPROVER_ROLE,),
        )
        if cursor.fetchone() is not None:
            raise SystemExit(f"{APPROVER_ROLE} must not be a member of any role")
        # A failing statement is logged by the server with its text; keep the password out.
        cursor.execute("SET LOCAL log_min_error_statement = panic")
        cursor.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(role, sql.Literal(password)))
        cursor.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(DATABASE), role)
        )
    connection.commit()
    return created


def main() -> None:
    secrets_dir = Path(os.environ.get("OPSKIT_SECRETS_DIR", "/run/kit-secrets"))
    host = os.environ.get("OPSKIT_DB_HOST", "postgres")
    superuser_password = (secrets_dir / "postgres_superuser_password").read_text().strip()
    approver_password = (secrets_dir / "opskit_approver_password").read_text().strip()
    with psycopg.connect(
        host=host, user="postgres", password=superuser_password, dbname="postgres"
    ) as connection:
        created = ensure_approver_role(connection, approver_password)
    print(f"db-roles: {APPROVER_ROLE} {'created' if created else 'already present'}")


if __name__ == "__main__":
    main()
