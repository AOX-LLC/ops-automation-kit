"""core: approver sessions can only be revoked, and only the approver side can touch them.

Before this, the requester role (the n8n-facing API) could read every session's `csrf_token` and
set `revoked_at` back to NULL, which brought a logged-out session back to life.

- A trigger (enabled always) lets `revoked_at` go from NULL to non-NULL once, never back, and sets
  it from the database clock. Nothing else in a session changes after it is created, and
  `created_at` is the database's. A new session needs a well-formed token and an expiry that is
  in the future and at most 30 days away.
- The requester role loses all access to the table. The session store runs on the approver role's
  connection, so no requester path needs `csrf_token` and no function or narrower grant is needed.

No stored row is changed.

Revision ID: core_0007
Revises: core_0006
"""

from alembic import op

revision = "core_0007"
down_revision = "core_0006"
branch_labels = None
depends_on = None

REQUESTER = "opskit_app"
APPROVER = "opskit_approver"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE FUNCTION core.approver_sessions_guard() RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            db_now timestamptz := statement_timestamp();
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'approver sessions are never deleted';
            END IF;

            IF TG_OP = 'INSERT' THEN
                IF NEW.revoked_at IS NOT NULL THEN
                    RAISE EXCEPTION 'a new session is not revoked';
                END IF;
                IF NEW.csrf_token !~ '^[A-Za-z0-9_-]{{16,128}}$' THEN
                    RAISE EXCEPTION 'the session token has a shape the application refuses';
                END IF;
                IF NEW.expires_at <= db_now
                   OR extract(epoch FROM NEW.expires_at) - extract(epoch FROM db_now)
                      > 30 * 86400 THEN
                    RAISE EXCEPTION 'a session expires in the future and within 30 days';
                END IF;
                NEW.created_at := db_now;
                RETURN NEW;
            END IF;

            IF (NEW.id, NEW.csrf_token, NEW.created_at, NEW.expires_at)
               IS DISTINCT FROM (OLD.id, OLD.csrf_token, OLD.created_at, OLD.expires_at) THEN
                RAISE EXCEPTION 'a session changes only by being revoked';
            END IF;
            IF OLD.revoked_at IS NOT NULL THEN
                IF NEW.revoked_at IS DISTINCT FROM OLD.revoked_at THEN
                    RAISE EXCEPTION 'a revoked session stays revoked';
                END IF;
            ELSIF NEW.revoked_at IS NOT NULL THEN
                NEW.revoked_at := db_now;
            END IF;
            RETURN NEW;
        END;
        $$;

        CREATE FUNCTION core.approver_sessions_no_truncate() RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            RAISE EXCEPTION 'approver sessions are never truncated';
        END;
        $$;

        CREATE TRIGGER approver_sessions_guard
            BEFORE INSERT OR UPDATE OR DELETE ON core.approver_sessions
            FOR EACH ROW EXECUTE FUNCTION core.approver_sessions_guard();
        CREATE TRIGGER approver_sessions_no_truncate
            BEFORE TRUNCATE ON core.approver_sessions
            FOR EACH STATEMENT EXECUTE FUNCTION core.approver_sessions_no_truncate();
        ALTER TABLE core.approver_sessions ENABLE ALWAYS TRIGGER approver_sessions_guard;
        ALTER TABLE core.approver_sessions ENABLE ALWAYS TRIGGER approver_sessions_no_truncate;

        REVOKE ALL ON core.approver_sessions FROM {REQUESTER};
        REVOKE ALL ON core.approver_sessions FROM {APPROVER};
        GRANT SELECT, INSERT ON core.approver_sessions TO {APPROVER};
        GRANT UPDATE (revoked_at) ON core.approver_sessions TO {APPROVER};
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DROP TRIGGER approver_sessions_no_truncate ON core.approver_sessions;
        DROP TRIGGER approver_sessions_guard ON core.approver_sessions;
        DROP FUNCTION core.approver_sessions_no_truncate();
        DROP FUNCTION core.approver_sessions_guard();
        REVOKE ALL ON core.approver_sessions FROM {APPROVER};
        -- NOTE: this gives the requester its old access back, including the un-revoke.
        GRANT SELECT, INSERT ON core.approver_sessions TO {REQUESTER};
        GRANT UPDATE (revoked_at) ON core.approver_sessions TO {REQUESTER};
        """
    )
