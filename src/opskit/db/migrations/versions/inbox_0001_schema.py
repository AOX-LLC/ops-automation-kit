"""inbox: empty schema; its tables arrive with the phase that builds the workflow.

Revision ID: inbox_0001
"""

from alembic import op

revision = "inbox_0001"
down_revision = None
branch_labels = ("inbox",)
depends_on = None

APP_ROLE = "opskit_app"


def upgrade() -> None:
    op.execute("CREATE SCHEMA inbox")
    op.execute(f"GRANT USAGE ON SCHEMA inbox TO {APP_ROLE}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA inbox "
        f"GRANT SELECT, INSERT, UPDATE ON TABLES TO {APP_ROLE}"
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA inbox CASCADE")
