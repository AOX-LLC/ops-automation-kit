"""core: the audit insert trigger bounds `occurred_at` and stamps `recorded_at` (agent-core v0.1.0).

- `recorded_at` is when the database wrote the row: set by the insert trigger from the database
  clock whatever the writer sent, like `db_role`. It is outside the record hash. Rows written
  before this revision have none.
- `occurred_at` may be supplied by the caller (agent-core's `AuditEvent.occurred_at`), so the same
  trigger refuses one more than 24 hours before or 5 minutes after the database clock. The hash
  still covers `occurred_at` and nothing else about time. Without a bound a writer could file an
  event under any date it liked.

The function is the one `core_0005` created, replaced; the trigger stays `ENABLE ALWAYS`. The
comparison is in epoch seconds, so it does not depend on the session time zone. `core_0010`'s
`audit_log_bounds` is untouched. No stored row changes.

Revision ID: core_0011
Revises: core_0010
"""

from alembic import op

revision = "core_0011"
down_revision = "core_0010"
branch_labels = None
depends_on = None

# Keep in step with aox_agent_core.audit.OCCURRED_AT_MAX_PAST and OCCURRED_AT_MAX_FUTURE.
MAX_PAST_SECONDS = 24 * 3600
MAX_FUTURE_SECONDS = 5 * 60


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE core.audit_log ADD COLUMN recorded_at timestamptz;

        CREATE OR REPLACE FUNCTION core.audit_log_set_db_role() RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            db_now timestamptz := statement_timestamp();
            skew double precision;
        BEGIN
            IF NEW.schema_version <> 3 THEN
                RAISE EXCEPTION 'new audit records must be schema 3';
            END IF;
            skew := extract(epoch FROM NEW.occurred_at) - extract(epoch FROM db_now);
            IF skew IS NULL OR skew > {MAX_FUTURE_SECONDS} OR skew < -{MAX_PAST_SECONDS} THEN
                RAISE EXCEPTION 'occurred_at must be within 24 hours before and 5 minutes after '
                    'the database clock';
            END IF;
            NEW.db_role := current_user::text;
            NEW.recorded_at := db_now;
            RETURN NEW;
        END;
        $$;
        """
    )


def downgrade() -> None:
    raise NotImplementedError("core_0011 is not reversible: records now carry recorded_at")
