"""receipts: empty schema; its tables arrive with the phase that builds the workflow.

Revision ID: receipts_0001
"""

from alembic import op

revision = "receipts_0001"
down_revision = None
branch_labels = ("receipts",)
depends_on = None

APP_ROLE = "opskit_app"


def upgrade() -> None:
    op.execute("CREATE SCHEMA receipts")
    op.execute(f"GRANT USAGE ON SCHEMA receipts TO {APP_ROLE}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA receipts "
        f"GRANT SELECT, INSERT, UPDATE ON TABLES TO {APP_ROLE}"
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA receipts CASCADE")
