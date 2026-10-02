"""leads: empty schema; its tables arrive with the phase that builds the workflow.

Revision ID: leads_0001
"""

from alembic import op

revision = "leads_0001"
down_revision = None
branch_labels = ("leads",)
depends_on = None

APP_ROLE = "opskit_app"


def upgrade() -> None:
    op.execute("CREATE SCHEMA leads")
    op.execute(f"GRANT USAGE ON SCHEMA leads TO {APP_ROLE}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA leads "
        f"GRANT SELECT, INSERT, UPDATE ON TABLES TO {APP_ROLE}"
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA leads CASCADE")
