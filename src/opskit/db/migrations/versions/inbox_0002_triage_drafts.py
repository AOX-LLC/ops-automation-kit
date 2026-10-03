"""inbox: fetched messages, triage results and drafted replies.

Revision ID: inbox_0002
"""

from alembic import op

revision = "inbox_0002"
down_revision = "inbox_0001"
branch_labels = None
depends_on = ("core_0003",)  # references core.runs and core.approvals, on another branch

APP_ROLE = "opskit_app"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE inbox.messages (
            message_id      text PRIMARY KEY,
            mailpit_id      text NOT NULL,
            from_header     text NOT NULL,
            reply_to_header text,
            to_addr         text,
            subject         text NOT NULL,
            received_at     timestamptz,
            body_text       text NOT NULL,
            fetched_at      timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE inbox.triage (
            message_id        text PRIMARY KEY REFERENCES inbox.messages (message_id),
            run_id            uuid NOT NULL REFERENCES core.runs (id),
            category          text NOT NULL,
            priority          text NOT NULL,
            needs_reply       boolean NOT NULL,
            escalate          boolean NOT NULL,
            route             text NOT NULL CHECK (route IN ('draft', 'quarantine', 'no_reply')),
            quarantined       boolean NOT NULL,
            injection_reasons jsonb NOT NULL DEFAULT '[]',
            replay_key        char(64),
            cost_usd          numeric(10, 6) NOT NULL DEFAULT 0,
            latency_ms        integer,
            triaged_at        timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE inbox.drafts (
            id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            message_id       text NOT NULL UNIQUE REFERENCES inbox.messages (message_id),
            run_id           uuid NOT NULL REFERENCES core.runs (id),
            approval_id      uuid REFERENCES core.approvals (id),
            to_addr          text NOT NULL,
            subject          text NOT NULL,
            in_reply_to      text,
            body             text NOT NULL,
            facts_used       jsonb NOT NULL DEFAULT '[]',
            grounding        jsonb NOT NULL DEFAULT '{}',
            reply_to_differs boolean NOT NULL DEFAULT false,
            status           text NOT NULL CHECK (
                status IN ('draft', 'pending', 'approved', 'rejected', 'expired', 'sent', 'failed')
            ),
            failure_reason   text,
            replay_key       char(64),
            cost_usd         numeric(10, 6) NOT NULL DEFAULT 0,
            latency_ms       integer,
            created_at       timestamptz NOT NULL DEFAULT now(),
            sent_at          timestamptz
        )
    """)
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON inbox.messages, inbox.triage, inbox.drafts TO {APP_ROLE}"
    )


def downgrade() -> None:
    op.execute("DROP TABLE inbox.drafts")
    op.execute("DROP TABLE inbox.triage")
    op.execute("DROP TABLE inbox.messages")
