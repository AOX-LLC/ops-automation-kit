"""core: audit schema 4 and the database-set login (agent-core v0.1.0).

agent-core v0.1.0 writes new audit records at schema version 4 (`AUDIT_SCHEMA_VERSION`); the hash
covers the same fields, so no chain is rehashed and records at 2 and 3 still verify. Without this
revision every audit write fails: the insert trigger demanded version 3.

- `schema_version` may be 2, 3 or 4 in the table; the insert trigger requires 4, so a writer
  cannot dodge a rule by claiming an older version.
- `db_login` is the login the connection authenticated as, `session_user`, set by the insert
  trigger whatever the writer sent, outside the hash, next to `db_role` (`current_user`).
  `SET ROLE` changes `db_role` and never `db_login`, so a row names the real login after a role
  switch. Rows written before this revision have none.
- `db_role` is set for versions 3 and up, `db_login` for 4 and up, and for no older row.

Login binding of `resolved_by`, which v0.1.0 offers its own approvals guard, is not adopted: the
kit's decision path already runs on its own approver credential (core_0006). The function is the
one `core_0011` left, replaced; the trigger stays `ENABLE ALWAYS`, the `occurred_at` bounds and
the `recorded_at` stamp are unchanged. No stored row changes.

Revision ID: core_0013
Revises: core_0012
"""

from alembic import op

revision = "core_0013"
down_revision = "core_0012"
branch_labels = None
depends_on = None

# Keep in step with aox_agent_core.audit.OCCURRED_AT_MAX_PAST and OCCURRED_AT_MAX_FUTURE.
MAX_PAST_SECONDS = 24 * 3600
MAX_FUTURE_SECONDS = 5 * 60


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE core.audit_log ADD COLUMN db_login text;
        ALTER TABLE core.audit_log DROP CONSTRAINT audit_log_schema_version_check;
        ALTER TABLE core.audit_log ADD CONSTRAINT audit_log_schema_version_check
            CHECK (schema_version IN (2, 3, 4));
        ALTER TABLE core.audit_log DROP CONSTRAINT audit_log_db_role_matches_version;
        ALTER TABLE core.audit_log ADD CONSTRAINT audit_log_db_role_matches_version
            CHECK ((schema_version >= 3) = (db_role IS NOT NULL));
        ALTER TABLE core.audit_log ADD CONSTRAINT audit_log_db_login_matches_version
            CHECK ((schema_version >= 4) = (db_login IS NOT NULL));

        CREATE OR REPLACE FUNCTION core.audit_log_set_db_role() RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            db_now timestamptz := statement_timestamp();
            skew double precision;
        BEGIN
            IF NEW.schema_version <> 4 THEN
                RAISE EXCEPTION 'new audit records must be schema 4';
            END IF;
            skew := extract(epoch FROM NEW.occurred_at) - extract(epoch FROM db_now);
            IF skew IS NULL OR skew > {MAX_FUTURE_SECONDS} OR skew < -{MAX_PAST_SECONDS} THEN
                RAISE EXCEPTION 'occurred_at must be within 24 hours before and 5 minutes after '
                    'the database clock';
            END IF;
            NEW.db_role := current_user::text;
            NEW.db_login := session_user::text;
            NEW.recorded_at := db_now;
            RETURN NEW;
        END;
        $$;
        """
    )


def downgrade() -> None:
    raise NotImplementedError("core_0013 is not reversible: version 4 records join the chain")
