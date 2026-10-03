"""leads: one researched-or-unresolved result per company per run.

Revision ID: leads_0002
"""

from alembic import op

revision = "leads_0002"
down_revision = "leads_0001"
branch_labels = None
# references core.runs and crm.accounts, on other branches
depends_on = ("core_0003", "crm_0002")

APP_ROLE = "opskit_app"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE leads.research (
            id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id       uuid NOT NULL REFERENCES core.runs (id),
            company_name text NOT NULL,
            city_hint    text NOT NULL,
            website      text,
            domain       text,
            status       text NOT NULL CHECK (status IN ('researched', 'unresolved')),
            reason       text,
            fields       jsonb NOT NULL,
            findings     jsonb NOT NULL DEFAULT '[]',
            pages        jsonb NOT NULL DEFAULT '[]',
            raw_cites    integer NOT NULL DEFAULT 0,
            valid_cites  integer NOT NULL DEFAULT 0,
            replay_key   char(64),
            cost_usd     numeric(10, 6) NOT NULL DEFAULT 0,
            latency_ms   integer,
            crm_action   text CHECK (crm_action IN ('created', 'updated')),
            account_id   uuid REFERENCES crm.accounts (id),
            created_at   timestamptz NOT NULL DEFAULT now(),
            UNIQUE (run_id, company_name, city_hint)
        )
    """)
    # The key (run, company, city) and the creation time never change; a re-run overwrites
    # only the result columns.
    op.execute(f"GRANT SELECT, INSERT ON leads.research TO {APP_ROLE}")
    op.execute(f"REVOKE UPDATE ON leads.research FROM {APP_ROLE}")
    op.execute(
        "GRANT UPDATE (website, domain, status, reason, fields, findings, pages, raw_cites, "
        f"valid_cites, replay_key, cost_usd, latency_ms, crm_action, account_id) "
        f"ON leads.research TO {APP_ROLE}"
    )


def downgrade() -> None:
    op.execute("DROP TABLE leads.research")
