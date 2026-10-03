"""receipts: extraction results and reconciliation runs.

Revision ID: receipts_0002
"""

from alembic import op

revision = "receipts_0002"
down_revision = "receipts_0001"
branch_labels = None
depends_on = ("core_0001",)  # references core.runs, on another branch

APP_ROLE = "opskit_app"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE receipts.extractions (
            sha256       char(64) PRIMARY KEY,
            path         text NOT NULL,
            run_id       uuid NOT NULL REFERENCES core.runs (id),
            status       text NOT NULL CHECK (status IN ('extracted', 'needs_review', 'failed')),
            reason       text,
            fields       jsonb,
            replay_key   char(64),
            tier         text,
            model        text,
            cost_usd     numeric(10, 6) NOT NULL DEFAULT 0,
            latency_ms   integer,
            extracted_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE receipts.reconciliations (
            id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id     uuid NOT NULL REFERENCES core.runs (id),
            rows       jsonb NOT NULL,
            summary    jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON receipts.extractions, receipts.reconciliations "
        f"TO {APP_ROLE}"
    )


def downgrade() -> None:
    op.execute("DROP TABLE receipts.reconciliations")
    op.execute("DROP TABLE receipts.extractions")
