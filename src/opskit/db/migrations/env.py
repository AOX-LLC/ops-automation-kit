"""Alembic environment: runs as the schema owner; one branch per domain (`upgrade heads`)."""

from __future__ import annotations

import logging

from alembic import context
from sqlalchemy import create_engine

from opskit.config import Settings

# Something in the import chain gives the alembic logger a handler, which stops Python printing
# warnings by itself: without this a migration's report (RAISE WARNING) never reaches the log.
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
settings = Settings()
log = logging.getLogger("alembic.runtime.migration")


def run_migrations_online() -> None:
    engine = create_engine(settings.database_url())
    with engine.connect() as connection:
        # Migrations report through RAISE WARNING (ids of requests they closed); show them.
        connection.connection.add_notice_handler(
            lambda diag: log.warning("%s", diag.message_primary)
        )
        context.configure(connection=connection, version_table_schema="public")
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
