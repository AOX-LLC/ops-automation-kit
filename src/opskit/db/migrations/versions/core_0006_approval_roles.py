"""core: split the approval database roles, enforced by a trigger (agent-core v0.1.0a3 design).

Two roles, never members of each other:

- the requester, `opskit_app` (the n8n-facing API): may insert a pending approval, withdraw
  it, expire it once due, and consume it once approved. It can never write decision columns.
- the approver, `opskit_approver` (the approver page's decision path only): may approve or
  reject a pending, unexpired approval, and expire a due one.

A trigger checks every insert, update and delete against that transition table, using the
database's own role names and clock, so direct SQL with the requester's credentials cannot
approve either. `opskit_approver` is created by the db-roles step before this runs.

Approvals already stored as approved and unused, with no `approval.decided` audit event, were
approved by something other than the decision path (the old app role could set status with
plain SQL). They are cancelled here, before the trigger exists, and counted in a notice. Ones
already consumed are only counted. The audit check rules out a plain status flip, not a
forged event: the old app role could also append audit rows.

Revision ID: core_0006
Revises: core_0005
"""

from alembic import op

revision = "core_0006"
down_revision = "core_0005"
branch_labels = None
depends_on = None

REQUESTER = "opskit_app"
APPROVER = "opskit_approver"


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APPROVER}') THEN
                RAISE EXCEPTION 'role {APPROVER} is missing: the db-roles step must run first';
            END IF;
            IF EXISTS (
                SELECT 1 FROM pg_roles
                WHERE rolname IN ('{REQUESTER}', '{APPROVER}')
                  AND (rolsuper OR rolcreaterole OR rolbypassrls OR rolreplication)
            ) THEN
                RAISE EXCEPTION 'the requester and approver roles must have no special rights';
            END IF;
            IF pg_has_role('{REQUESTER}', '{APPROVER}', 'MEMBER')
               OR pg_has_role('{APPROVER}', '{REQUESTER}', 'MEMBER') THEN
                RAISE EXCEPTION 'the requester and approver roles must not share membership';
            END IF;
        END $$;

        -- Approved, unused, and no decision event: not from the decision path. Cancel them.
        DO $$
        DECLARE
            cancelled integer;
            already_used integer;
        BEGIN
            UPDATE core.approvals a
            SET status = 'cancelled', closed_at = now(), decision = NULL,
                resolved_by = NULL, resolved_at = NULL,
                reason = left('closed by the role split: no approval.decided event; was marked '
                              || 'approved by ' || coalesce(a.resolved_by, 'unknown'), 500)
            WHERE a.status = 'approved'
              AND NOT EXISTS (
                  SELECT 1 FROM core.audit_log e
                  WHERE e.action = 'approval.decided' AND e.subject_id = a.id::text
                    AND e.payload::jsonb ->> 'decision' = 'approve')
              AND NOT EXISTS (
                  SELECT 1 FROM core.audit_log_v1 v
                  WHERE v.subject_id = a.id::text AND v.action LIKE 'approval.%'
                    AND v.action <> 'approval.requested');
            GET DIAGNOSTICS cancelled = ROW_COUNT;
            RAISE NOTICE 'role split: % unaudited approved request(s) cancelled', cancelled;
            -- A consumed request was already used; it cannot be undone, only reported.
            SELECT count(*) INTO already_used FROM core.approvals a
            WHERE a.status = 'consumed'
              AND NOT EXISTS (
                  SELECT 1 FROM core.audit_log e
                  WHERE e.action = 'approval.decided' AND e.subject_id = a.id::text
                    AND e.payload::jsonb ->> 'decision' = 'approve')
              AND NOT EXISTS (
                  SELECT 1 FROM core.audit_log_v1 v
                  WHERE v.subject_id = a.id::text AND v.action LIKE 'approval.%'
                    AND v.action <> 'approval.requested');
            RAISE NOTICE 'role split: % consumed request(s) have no decision event; review them',
                already_used;
        END $$;
        """  # noqa: S608 - only the two fixed role names are interpolated
    )
    op.execute(
        f"""
        CREATE FUNCTION core.approvals_guard() RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            -- session_user too, so SET ROLE cannot cross sides (a superuser would have to log
            -- in as the approver, which needs its password).
            as_approver boolean := current_user = '{APPROVER}' AND session_user = '{APPROVER}';
            as_requester boolean := current_user = '{REQUESTER}' AND session_user = '{REQUESTER}';
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'approvals are never deleted';
            END IF;

            IF TG_OP = 'INSERT' THEN
                IF NOT as_requester THEN
                    RAISE EXCEPTION 'only the requester role may create an approval';
                END IF;
                IF NEW.status <> 'pending' OR NEW.decision IS NOT NULL
                   OR NEW.resolved_by IS NOT NULL OR NEW.resolved_at IS NOT NULL
                   OR NEW.consumed_at IS NOT NULL OR NEW.closed_at IS NOT NULL THEN
                    RAISE EXCEPTION 'a new approval must be pending and undecided';
                END IF;
                IF NEW.expires_at <= NEW.created_at
                   OR NEW.expires_at - NEW.created_at > interval '7 days'
                   OR NEW.created_at > now() + interval '5 minutes' THEN
                    RAISE EXCEPTION 'an approval lives at most 7 days from a current created_at';
                END IF;
                RETURN NEW;
            END IF;

            -- UPDATE: what a request is never changes.
            IF (NEW.id, NEW.run_id, NEW.action, NEW.summary, NEW.payload, NEW.payload_sha256,
                NEW.requested_by, NEW.required_role, NEW.resume_url, NEW.created_at,
                NEW.expires_at, NEW.run_context, NEW.delegates)
               IS DISTINCT FROM
               (OLD.id, OLD.run_id, OLD.action, OLD.summary, OLD.payload, OLD.payload_sha256,
                OLD.requested_by, OLD.required_role, OLD.resume_url, OLD.created_at,
                OLD.expires_at, OLD.run_context, OLD.delegates) THEN
                RAISE EXCEPTION 'identity, payload, lifetime and delegates are fixed';
            END IF;
            IF NEW.status = OLD.status THEN
                IF NEW IS DISTINCT FROM OLD THEN
                    RAISE EXCEPTION 'an approval changes only by moving to a new status';
                END IF;
                RETURN NEW;
            END IF;

            IF OLD.status = 'pending' AND NEW.status IN ('approved', 'rejected') THEN
                IF NOT as_approver THEN
                    RAISE EXCEPTION 'only the approver role may approve or reject';
                END IF;
                IF NEW.decision IS DISTINCT FROM (CASE NEW.status WHEN 'approved' THEN 'approve'
                                                                  ELSE 'reject' END)
                   OR NEW.resolved_by IS NULL OR NEW.resolved_at IS NULL
                   OR NEW.resolved_by = OLD.requested_by
                   OR NEW.consumed_at IS NOT NULL OR NEW.closed_at IS NOT NULL THEN
                    RAISE EXCEPTION 'a decision needs a matching decision and a new approver';
                END IF;
                IF OLD.expires_at <= now() THEN
                    RAISE EXCEPTION 'the approval has expired';
                END IF;
            ELSIF OLD.status = 'pending' AND NEW.status = 'cancelled' THEN
                IF NOT as_requester THEN
                    RAISE EXCEPTION 'only the requester role may cancel';
                END IF;
                IF NEW.closed_at IS NULL OR (NEW.decision, NEW.resolved_by, NEW.resolved_at,
                       NEW.reason, NEW.consumed_at)
                       IS DISTINCT FROM
                       (OLD.decision, OLD.resolved_by, OLD.resolved_at, OLD.reason,
                        OLD.consumed_at) THEN
                    RAISE EXCEPTION 'a cancellation sets closed_at and nothing else';
                END IF;
            ELSIF OLD.status = 'pending' AND NEW.status = 'expired' THEN
                IF NOT (as_requester OR as_approver) THEN
                    RAISE EXCEPTION 'only the requester or approver role may expire';
                END IF;
                IF OLD.expires_at > now() THEN
                    RAISE EXCEPTION 'the approval has not reached its expiry';
                END IF;
                IF NEW.closed_at IS NULL OR (NEW.decision, NEW.resolved_by, NEW.resolved_at,
                       NEW.reason, NEW.consumed_at)
                       IS DISTINCT FROM
                       (OLD.decision, OLD.resolved_by, OLD.resolved_at, OLD.reason,
                        OLD.consumed_at) THEN
                    RAISE EXCEPTION 'an expiry sets closed_at and nothing else';
                END IF;
            ELSIF OLD.status = 'approved' AND NEW.status = 'consumed' THEN
                IF NOT as_requester THEN
                    RAISE EXCEPTION 'only the requester role may consume';
                END IF;
                IF OLD.expires_at <= now() THEN
                    RAISE EXCEPTION 'the approval has expired';
                END IF;
                IF NEW.consumed_at IS NULL OR (NEW.decision, NEW.resolved_by, NEW.resolved_at,
                       NEW.reason, NEW.closed_at)
                       IS DISTINCT FROM
                       (OLD.decision, OLD.resolved_by, OLD.resolved_at, OLD.reason,
                        OLD.closed_at) THEN
                    RAISE EXCEPTION 'a consumption sets consumed_at and nothing else';
                END IF;
            ELSE
                RAISE EXCEPTION 'an approval cannot move from % to %', OLD.status, NEW.status;
            END IF;
            RETURN NEW;
        END;
        $$;

        CREATE FUNCTION core.approvals_no_truncate() RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            RAISE EXCEPTION 'approvals are never truncated';
        END;
        $$;

        CREATE TRIGGER approvals_guard
            BEFORE INSERT OR UPDATE OR DELETE ON core.approvals
            FOR EACH ROW EXECUTE FUNCTION core.approvals_guard();
        CREATE TRIGGER approvals_no_truncate
            BEFORE TRUNCATE ON core.approvals
            FOR EACH STATEMENT EXECUTE FUNCTION core.approvals_no_truncate();
        -- Enforced even when a session sets session_replication_role = replica.
        ALTER TABLE core.approvals ENABLE ALWAYS TRIGGER approvals_guard;
        ALTER TABLE core.approvals ENABLE ALWAYS TRIGGER approvals_no_truncate;

        -- Grants: the requester keeps its table-wide UPDATE no longer.
        REVOKE ALL ON core.approvals FROM {REQUESTER};
        GRANT SELECT, INSERT ON core.approvals TO {REQUESTER};
        GRANT UPDATE (status, consumed_at, closed_at) ON core.approvals TO {REQUESTER};

        GRANT USAGE ON SCHEMA core TO {APPROVER};
        REVOKE ALL ON core.approvals FROM {APPROVER};
        GRANT SELECT ON core.approvals TO {APPROVER};
        GRANT UPDATE (status, decision, resolved_by, resolved_at, reason, closed_at)
            ON core.approvals TO {APPROVER};
        GRANT SELECT, INSERT ON core.outbox TO {APPROVER};
        GRANT SELECT, INSERT ON core.audit_log TO {APPROVER};
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DROP TRIGGER approvals_no_truncate ON core.approvals;
        DROP TRIGGER approvals_guard ON core.approvals;
        DROP FUNCTION core.approvals_no_truncate();
        DROP FUNCTION core.approvals_guard();
        REVOKE ALL ON core.approvals, core.outbox, core.audit_log FROM {APPROVER};
        REVOKE USAGE ON SCHEMA core FROM {APPROVER};
        GRANT SELECT, INSERT, UPDATE ON core.approvals TO {REQUESTER};
        """
    )
