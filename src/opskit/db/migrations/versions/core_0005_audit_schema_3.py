"""core: audit schema 3 (agent-core v0.1.0a3) and the database-set writer role.

- schema_version may be 2 or 3 in the table, so records already in the chain keep version 2
  and still verify. New rows must be version 3: the insert trigger refuses version 2, so a
  writer cannot dodge attribution by claiming the older schema.
- db_role is the database role that inserted the row. The insert trigger sets it from
  current_user whatever was sent, and the table's update trigger keeps it fixed afterwards.
  It is outside the record hash. Version 2 rows already stored have none.

Both the requester and the approver role append audit rows, and `actor_id` is supplied by the
application, so db_role is what shows who really wrote a record.

Revision ID: core_0005
Revises: core_0004
"""

from alembic import op

revision = "core_0005"
down_revision = "core_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE core.audit_log ADD COLUMN db_role text;
        ALTER TABLE core.audit_log DROP CONSTRAINT audit_log_schema_version_check;
        ALTER TABLE core.audit_log ADD CONSTRAINT audit_log_schema_version_check
            CHECK (schema_version IN (2, 3));
        ALTER TABLE core.audit_log ADD CONSTRAINT audit_log_db_role_matches_version
            CHECK ((schema_version = 3) = (db_role IS NOT NULL));

        CREATE FUNCTION core.audit_log_set_db_role() RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF NEW.schema_version <> 3 THEN
                RAISE EXCEPTION 'new audit records must be schema 3';
            END IF;
            NEW.db_role := current_user::text;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER audit_log_db_role
            BEFORE INSERT ON core.audit_log
            FOR EACH ROW EXECUTE FUNCTION core.audit_log_set_db_role();
        ALTER TABLE core.audit_log ENABLE ALWAYS TRIGGER audit_log_db_role;
        """
    )


def downgrade() -> None:
    raise NotImplementedError("core_0005 is not reversible: version 3 records join the chain")
