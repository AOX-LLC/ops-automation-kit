"""core: align approvals and audit with agent-core v0.1.0a2.

- Approvals take agent-core's field names and a principal model; edited_subject is dropped
  (it breaks agent-core's payload binding). The payload stays stored so the approver can
  read what they approve; payload_sha256 binds consume() to it.
- The audit log becomes agent-core's hash-chained schema 2. The Phase 1 table is kept,
  read-only, as core.audit_log_v1; the new core.audit_log keeps its append-only triggers
  and the app role's insert-and-select-only grant.
- Run and model-call modes follow agent-core: replay | live | record.

Revision ID: core_0003
Revises: core_0002
"""

from alembic import op

revision = "core_0003"
down_revision = "core_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        -- Runs and model calls: agent-core's mode names.
        ALTER TABLE core.runs DROP CONSTRAINT runs_mode_check;
        UPDATE core.runs SET mode = 'replay' WHERE mode = 'mock';
        ALTER TABLE core.runs ADD CONSTRAINT runs_mode_check
            CHECK (mode IN ('replay', 'live', 'record'));
        ALTER TABLE core.model_calls DROP CONSTRAINT model_calls_mode_check;
        UPDATE core.model_calls SET mode = 'replay' WHERE mode = 'mock';
        ALTER TABLE core.model_calls ADD CONSTRAINT model_calls_mode_check
            CHECK (mode IN ('replay', 'live', 'record'));
        ALTER TABLE core.model_calls RENAME COLUMN fixture_key TO replay_key;
        ALTER TABLE core.model_calls ADD COLUMN model text;

        -- Approvals: agent-core's ApprovalRequest fields.
        ALTER TABLE core.approvals DROP CONSTRAINT approvals_check;
        ALTER TABLE core.approvals DROP CONSTRAINT approvals_status_check;
        ALTER TABLE core.approvals DROP COLUMN edited_subject;
        ALTER TABLE core.approvals RENAME COLUMN kind TO action;
        ALTER TABLE core.approvals RENAME COLUMN subject TO payload;
        ALTER TABLE core.approvals RENAME COLUMN requested_at TO created_at;
        ALTER TABLE core.approvals RENAME COLUMN decided_at TO resolved_at;
        ALTER TABLE core.approvals RENAME COLUMN decided_by TO resolved_by;
        ALTER TABLE core.approvals RENAME COLUMN decision_note TO reason;
        ALTER TABLE core.approvals ALTER COLUMN run_id DROP NOT NULL;
        ALTER TABLE core.approvals ALTER COLUMN resume_url DROP NOT NULL;
        ALTER TABLE core.approvals
            ADD COLUMN summary text NOT NULL DEFAULT 'approval requested',
            ADD COLUMN payload_sha256 char(64) NOT NULL DEFAULT repeat('0', 64),
            ADD COLUMN requested_by text NOT NULL DEFAULT 'service.n8n',
            ADD COLUMN required_role text NOT NULL DEFAULT 'approver',
            ADD COLUMN decision text CHECK (decision IN ('approve', 'reject')),
            ADD COLUMN consumed_at timestamptz,
            ADD COLUMN run_context jsonb;
        ALTER TABLE core.approvals ALTER COLUMN summary DROP DEFAULT;
        ALTER TABLE core.approvals ALTER COLUMN payload_sha256 DROP DEFAULT;
        ALTER TABLE core.approvals ALTER COLUMN requested_by DROP DEFAULT;
        ALTER TABLE core.approvals ALTER COLUMN required_role DROP DEFAULT;
        -- Phase 1 rows: map old outcomes onto agent-core's shape.
        UPDATE core.approvals SET decision = 'approve' WHERE status = 'approved';
        UPDATE core.approvals SET decision = 'reject' WHERE status = 'rejected';
        UPDATE core.approvals SET resolved_at = NULL, resolved_by = NULL WHERE status = 'expired';
        ALTER TABLE core.approvals ADD CONSTRAINT approvals_status_check CHECK (
            status IN ('pending', 'approved', 'rejected', 'consumed', 'expired', 'cancelled'));
        -- The decision each status implies, and resolved_* set exactly with a decision.
        ALTER TABLE core.approvals ADD CONSTRAINT approvals_decision_matches_status CHECK (
            (status IN ('approved', 'consumed') AND decision = 'approve')
            OR (status = 'rejected' AND decision = 'reject')
            OR (status IN ('pending', 'expired', 'cancelled') AND decision IS NULL));
        ALTER TABLE core.approvals ADD CONSTRAINT approvals_resolved_with_decision CHECK (
            (decision IS NULL) = (resolved_at IS NULL)
            AND (decision IS NULL) = (resolved_by IS NULL));
        ALTER TABLE core.approvals ADD CONSTRAINT approvals_consumed_at_matches CHECK (
            (status = 'consumed') = (consumed_at IS NOT NULL));
        ALTER TABLE core.approvals ADD CONSTRAINT approvals_no_self_approval CHECK (
            resolved_by IS NULL OR resolved_by <> requested_by);
        DROP INDEX core.approvals_pending_idx;
        CREATE INDEX approvals_pending_idx ON core.approvals (created_at, id)
            WHERE status = 'pending';

        -- Audit: keep Phase 1 rows read-only, start agent-core's hash chain.
        ALTER TABLE core.audit_log RENAME TO audit_log_v1;
        ALTER INDEX core.audit_log_run_idx RENAME TO audit_log_v1_run_idx;
        REVOKE INSERT ON core.audit_log_v1 FROM opskit_app;

        CREATE TABLE core.audit_log (
            seq             bigint PRIMARY KEY CHECK (seq >= 1),
            schema_version  smallint NOT NULL CHECK (schema_version = 2),
            event_id        uuid NOT NULL UNIQUE,
            occurred_at     timestamptz NOT NULL,
            action          text NOT NULL,
            actor_id        text NOT NULL,
            subject_id      text,
            payload         text NOT NULL,
            run_context     text,
            prev_hash       char(64) NOT NULL,
            record_hash     char(64) NOT NULL UNIQUE
        );
        CREATE INDEX audit_log_action_subject_idx ON core.audit_log (action, subject_id);
        CREATE TRIGGER audit_log_no_update_delete
            BEFORE UPDATE OR DELETE ON core.audit_log
            FOR EACH ROW EXECUTE FUNCTION core.reject_audit_mutation();
        CREATE TRIGGER audit_log_no_truncate
            BEFORE TRUNCATE ON core.audit_log
            FOR EACH STATEMENT EXECUTE FUNCTION core.reject_audit_mutation();
        GRANT SELECT, INSERT ON core.audit_log TO opskit_app;
        """
    )


def downgrade() -> None:
    raise NotImplementedError("core_0003 is not reversible: the audit hash chain starts here")
