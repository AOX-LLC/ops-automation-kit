"""crm: the demo CRM, a local table so the kit runs without any account.

Revision ID: crm_0001
"""

from alembic import op

revision = "crm_0001"
down_revision = None
branch_labels = ("crm",)
depends_on = None

APP_ROLE = "opskit_app"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE SCHEMA crm;
        GRANT USAGE ON SCHEMA crm TO {APP_ROLE};

        CREATE TABLE crm.accounts (
            id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name           text NOT NULL,
            domain         text UNIQUE,
            industry       text,
            employee_band  text,
            hq_city        text,
            description    text,
            created_at     timestamptz NOT NULL DEFAULT now(),
            updated_at     timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE crm.account_sources (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            account_id  uuid NOT NULL REFERENCES crm.accounts (id),
            field       text NOT NULL,
            source_ref  text NOT NULL,
            excerpt     text NOT NULL,
            found_at    timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX account_sources_account_idx ON crm.account_sources (account_id);

        GRANT SELECT, INSERT, UPDATE ON crm.accounts, crm.account_sources TO {APP_ROLE};
        """
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA crm CASCADE")
