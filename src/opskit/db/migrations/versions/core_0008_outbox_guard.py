"""core: a resume can be queued only for a decided approval, and delivery bookkeeping is bounded.

The approver role inserts outbox rows (the resume of a waiting n8n execution), and nothing stopped
it inserting any row at all: for an approval that is still pending, for a decision the approval
does not hold, or one already marked delivered.

- Insert (approver role only): the approval exists, has a resume URL, and is stored as approved or
  rejected; the payload is exactly `{"approval_id": <that id>, "decision": <the stored decision>}`;
  `delivered_at` is NULL, `attempts` is 0 and `last_error` is NULL. The approval's update and this
  insert run in one transaction, so the check sees the new status. `next_attempt_at` is the
  database clock.
- Update (the requester's worker): what a row says never changes; `attempts` only goes up, to at
  most 10; `last_error` is at most 200 characters; `next_attempt_at` is at most an hour ahead of the
  database clock; `delivered_at` goes from NULL to non-NULL once and is stamped by the database.

No stored row is changed, and rows already stored are never re-checked.

Revision ID: core_0008
Revises: core_0007
"""

from alembic import op

revision = "core_0008"
down_revision = "core_0007"
branch_labels = None
depends_on = None

APPROVER = "opskit_approver"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE FUNCTION core.outbox_guard() RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            db_now timestamptz := statement_timestamp();
            approval record;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NOT (current_user = '{APPROVER}' AND session_user = '{APPROVER}') THEN
                    RAISE EXCEPTION 'only the approver role may queue a resume';
                END IF;
                SELECT a.status, a.resume_url INTO approval
                FROM core.approvals a WHERE a.id = NEW.approval_id;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'no such approval to resume';
                END IF;
                IF approval.status NOT IN ('approved', 'rejected') THEN
                    RAISE EXCEPTION 'only an approved or rejected approval has a resume to queue';
                END IF;
                IF approval.resume_url IS NULL THEN
                    RAISE EXCEPTION 'an approval with no resume URL has nothing to resume';
                END IF;
                IF NEW.payload IS DISTINCT FROM jsonb_build_object(
                       'approval_id', NEW.approval_id::text,
                       'decision', CASE approval.status WHEN 'approved' THEN 'approve'
                                                        ELSE 'reject' END) THEN
                    RAISE EXCEPTION 'the resume must name this approval and its stored decision';
                END IF;
                IF NEW.delivered_at IS NOT NULL OR NEW.attempts <> 0
                   OR NEW.last_error IS NOT NULL THEN
                    RAISE EXCEPTION 'a new resume is undelivered, untried and without an error';
                END IF;
                NEW.next_attempt_at := db_now;
                RETURN NEW;
            END IF;

            IF (NEW.id, NEW.approval_id, NEW.payload)
               IS DISTINCT FROM (OLD.id, OLD.approval_id, OLD.payload) THEN
                RAISE EXCEPTION 'what a resume says is fixed';
            END IF;
            IF NEW.attempts < OLD.attempts OR NEW.attempts > 10 THEN
                RAISE EXCEPTION 'attempts only go up, to at most 10';
            END IF;
            IF length(NEW.last_error) > 200 THEN
                RAISE EXCEPTION 'last_error is at most 200 characters';
            END IF;
            IF extract(epoch FROM NEW.next_attempt_at) - extract(epoch FROM db_now) > 3600 THEN
                RAISE EXCEPTION 'the next attempt is at most an hour away';
            END IF;
            IF OLD.delivered_at IS NOT NULL THEN
                IF NEW.delivered_at IS DISTINCT FROM OLD.delivered_at THEN
                    RAISE EXCEPTION 'a delivered resume stays delivered';
                END IF;
            ELSIF NEW.delivered_at IS NOT NULL THEN
                NEW.delivered_at := db_now;
            END IF;
            RETURN NEW;
        END;
        $$;

        CREATE TRIGGER outbox_guard
            BEFORE INSERT OR UPDATE ON core.outbox
            FOR EACH ROW EXECUTE FUNCTION core.outbox_guard();
        ALTER TABLE core.outbox ENABLE ALWAYS TRIGGER outbox_guard;
        """  # noqa: S608 - only the fixed approver role name is interpolated
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER outbox_guard ON core.outbox;
        DROP FUNCTION core.outbox_guard();
        """
    )
