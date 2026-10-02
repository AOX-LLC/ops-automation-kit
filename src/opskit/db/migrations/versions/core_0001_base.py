"""core: runs, approvals, outbox, append-only audit log, model calls, sample manifest.

Revision ID: core_0001
"""

from alembic import op

revision = "core_0001"
down_revision = None
branch_labels = ("core",)
depends_on = None

APP_ROLE = "opskit_app"


def upgrade() -> None:
    op.execute("CREATE SCHEMA core")
    op.execute(f"GRANT USAGE ON SCHEMA core TO {APP_ROLE}")
    op.execute(
        """
        CREATE TABLE core.runs (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workflow          text NOT NULL
                              CHECK (workflow IN ('kit_smoke', 'receipts', 'leads', 'inbox')),
            n8n_workflow_id   text,
            n8n_execution_id  text UNIQUE,
            mode              text NOT NULL CHECK (mode IN ('mock', 'live', 'record')),
            status            text NOT NULL DEFAULT 'running'
                              CHECK (status IN ('running', 'waiting', 'succeeded', 'failed')),
            started_at        timestamptz NOT NULL DEFAULT now(),
            finished_at       timestamptz
        );

        CREATE TABLE core.approvals (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id          uuid NOT NULL REFERENCES core.runs (id),
            kind            text NOT NULL,
            subject         jsonb NOT NULL,
            edited_subject  jsonb,
            status          text NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
            resume_url      text NOT NULL,
            requested_at    timestamptz NOT NULL DEFAULT now(),
            expires_at      timestamptz NOT NULL,
            decided_at      timestamptz,
            decided_by      text,
            decision_note   text,
            CHECK ((status = 'pending') = (decided_at IS NULL))
        );
        CREATE INDEX approvals_pending_idx ON core.approvals (requested_at, id)
            WHERE status = 'pending';
        CREATE INDEX approvals_expiry_idx ON core.approvals (expires_at)
            WHERE status = 'pending';

        CREATE TABLE core.outbox (
            id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            approval_id      uuid NOT NULL UNIQUE REFERENCES core.approvals (id),
            payload          jsonb NOT NULL,
            attempts         integer NOT NULL DEFAULT 0,
            next_attempt_at  timestamptz NOT NULL DEFAULT now(),
            delivered_at     timestamptz,
            last_error       text
        );
        CREATE INDEX outbox_due_idx ON core.outbox (next_attempt_at)
            WHERE delivered_at IS NULL;

        CREATE TABLE core.audit_log (
            id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            at            timestamptz NOT NULL DEFAULT now(),
            run_id        uuid REFERENCES core.runs (id),
            actor         text NOT NULL,
            action        text NOT NULL,
            subject_type  text,
            subject_id    text,
            details       jsonb NOT NULL DEFAULT '{}'
        );
        CREATE INDEX audit_log_run_idx ON core.audit_log (run_id);

        CREATE FUNCTION core.reject_audit_mutation() RETURNS trigger
            LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'core.audit_log is append-only';
        END;
        $$;
        CREATE TRIGGER audit_log_no_update_delete
            BEFORE UPDATE OR DELETE ON core.audit_log
            FOR EACH ROW EXECUTE FUNCTION core.reject_audit_mutation();
        CREATE TRIGGER audit_log_no_truncate
            BEFORE TRUNCATE ON core.audit_log
            FOR EACH STATEMENT EXECUTE FUNCTION core.reject_audit_mutation();

        CREATE TABLE core.model_calls (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id          uuid REFERENCES core.runs (id),
            prompt_id       text NOT NULL,
            prompt_version  integer NOT NULL,
            tier            text NOT NULL CHECK (tier IN ('small', 'mid', 'large')),
            fixture_key     char(64) NOT NULL,
            mode            text NOT NULL CHECK (mode IN ('mock', 'live', 'record')),
            input_tokens    integer NOT NULL,
            output_tokens   integer NOT NULL,
            cost_usd        numeric(10, 6) NOT NULL,
            latency_ms      integer NOT NULL,
            created_at      timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE core.sample_files (
            path       text PRIMARY KEY,
            workflow   text NOT NULL,
            kind       text NOT NULL,
            sha256     char(64) NOT NULL,
            bytes      integer NOT NULL,
            loaded_at  timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        f"""
        GRANT SELECT, INSERT, UPDATE ON core.runs, core.approvals, core.outbox,
            core.model_calls, core.sample_files TO {APP_ROLE};
        GRANT SELECT, INSERT ON core.audit_log TO {APP_ROLE};
        """
    )


def downgrade() -> None:
    op.execute("DROP SCHEMA core CASCADE")
