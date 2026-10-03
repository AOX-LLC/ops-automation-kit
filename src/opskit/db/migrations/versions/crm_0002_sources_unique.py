"""crm: founded year on accounts; one source row per (account, field).

Revision ID: crm_0002
"""

from alembic import op

revision = "crm_0002"
down_revision = "crm_0001"
branch_labels = None
depends_on = None


APP_ROLE = "opskit_app"


def upgrade() -> None:
    op.execute("ALTER TABLE crm.accounts ADD COLUMN founded_year integer")
    # Keep the newest source per (account, field). The migration runs as the owner, which
    # the app role (no DELETE) is not.
    op.execute("""
        DELETE FROM crm.account_sources AS s
        USING (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY account_id, field ORDER BY found_at DESC, id DESC
                   ) AS newest_first
            FROM crm.account_sources
        ) AS ranked
        WHERE s.id = ranked.id AND ranked.newest_first > 1
    """)
    op.execute("""
        ALTER TABLE crm.account_sources
            ADD CONSTRAINT account_sources_account_field_key UNIQUE (account_id, field)
    """)
    # A source row's account and field are its identity; only what it cites may be updated.
    op.execute(f"REVOKE UPDATE ON crm.account_sources FROM {APP_ROLE}")
    op.execute(f"GRANT UPDATE (source_ref, excerpt, found_at) ON crm.account_sources TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"GRANT UPDATE ON crm.account_sources TO {APP_ROLE}")
    op.execute("ALTER TABLE crm.account_sources DROP CONSTRAINT account_sources_account_field_key")
    op.execute("ALTER TABLE crm.accounts DROP COLUMN founded_year")
