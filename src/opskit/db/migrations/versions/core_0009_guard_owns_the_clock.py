"""core: the approvals guard stamps every time itself and measures lifetime on the database clock.

`resolved_at`, `closed_at`, `consumed_at` and `created_at` were supplied by the caller and merely
checked (`created_at` within five minutes of the database clock). A caller could therefore choose
when a decision, a cancellation or a consumption appeared to happen. Now the guard ignores what the
caller sends and sets:

| event             | set to                                                                   |
| ----------------- | ------------------------------------------------------------------------ |
| insert            | `created_at` = statement_timestamp(); `expires_at` = that + the caller's |
|                   | own lifetime (`expires_at - created_at`, which must be in (0, 7 days])   |
| approve or reject | `resolved_at` = statement_timestamp()                                    |
| consume           | `consumed_at` = statement_timestamp()                                    |
| cancel            | `closed_at`   = statement_timestamp()                                    |
| expire            | `closed_at`   = the row's own stored `expires_at`                        |

An expiry keeps `closed_at = expires_at`: that is the stored row's value, not the caller's, and the
queue reports a due pending row the same way before the sweep stores it, so a request reads the same
either side of the sweep. The lifetime is the only thing taken from the caller's clock, as a length,
never as a point in time, so a skewed api clock cannot move a deadline.

The function is replaced as it was in core_0006 with those changes only. No stored row is changed.

Revision ID: core_0009
Revises: core_0008
"""

from alembic import op

revision = "core_0009"
down_revision = "core_0008"
branch_labels = None
depends_on = None

REQUESTER = "opskit_app"
APPROVER = "opskit_approver"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION core.approvals_guard() RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            -- session_user too, so SET ROLE cannot cross sides. The owner and a superuser are
            -- trusted all the same: either can disable this trigger or take on the approver's
            -- identity (SET SESSION AUTHORIZATION).
            as_approver boolean := current_user = '{APPROVER}' AND session_user = '{APPROVER}';
            as_requester boolean := current_user = '{REQUESTER}' AND session_user = '{REQUESTER}';
            -- statement_timestamp(), not now(): a transaction opened before expiry must not
            -- carry an old clock past it. Every time check compares instants or epoch seconds:
            -- none uses date or interval arithmetic, which depends on the session TimeZone.
            db_now timestamptz := statement_timestamp();
            lifetime double precision;
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
                IF NEW.reason IS NOT NULL THEN
                    RAISE EXCEPTION 'a new approval has no reason yet';
                END IF;
                -- Only the length of the lifetime comes from the caller; both ends are then the
                -- database's. An infinite value gives an infinite length, which is refused.
                lifetime := extract(epoch FROM NEW.expires_at)
                            - extract(epoch FROM NEW.created_at);
                IF lifetime IS NULL OR lifetime <= 0 OR lifetime > 604800 THEN
                    RAISE EXCEPTION 'an approval lives at most 7 days';
                END IF;
                NEW.created_at := db_now;
                NEW.expires_at := db_now + lifetime * interval '1 second';
                -- Field shapes, matched to the application's models as far as SQL can: a row
                -- that fails them is skipped by the approver's listing (and its paging counts
                -- fetched rows, not valid ones), but refusing it here keeps it out entirely.
                IF NEW.action !~ '^[a-z][a-z0-9_]*([.][a-z][a-z0-9_]*)*$'
                   OR length(NEW.action) > 100
                   OR length(NEW.summary) NOT BETWEEN 1 AND 500
                   OR NEW.requested_by !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{{0,127}}$'
                   OR NEW.required_role !~ '^[a-z][a-z0-9_.-]{{0,63}}$'
                   OR NEW.payload_sha256 !~ '^[0-9a-f]{{64}}$'
                   OR jsonb_typeof(NEW.delegates) <> 'array'
                   OR jsonb_array_length(NEW.delegates) > 16
                   OR EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.delegates) AS d(v)
                              WHERE jsonb_typeof(d.v) <> 'string'
                                 OR (d.v #>> '{{}}') !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{{0,127}}$')
                   OR (NEW.run_context IS NOT NULL AND (
                          jsonb_typeof(NEW.run_context) <> 'object'
                          OR jsonb_typeof(NEW.run_context -> 'run_id') IS DISTINCT FROM 'string'
                          OR (NEW.run_context ->> 'run_id')
                             !~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{{0,199}}$'
                          OR (NEW.run_context - 'run_id' - 'external_ids') <> '{{}}'::jsonb))
                THEN
                    RAISE EXCEPTION 'the approval has a field in a shape the application refuses';
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
                   OR NEW.resolved_by IS NULL
                   OR NEW.resolved_by = OLD.requested_by
                   OR NEW.consumed_at IS NOT NULL OR NEW.closed_at IS NOT NULL THEN
                    RAISE EXCEPTION 'a decision needs a matching decision and a new approver';
                END IF;
                IF OLD.expires_at <= db_now THEN
                    RAISE EXCEPTION 'the approval has expired';
                END IF;
                NEW.resolved_at := db_now;
            ELSIF OLD.status = 'pending' AND NEW.status = 'cancelled' THEN
                IF NOT as_requester THEN
                    RAISE EXCEPTION 'only the requester role may cancel';
                END IF;
                IF (NEW.decision, NEW.resolved_by, NEW.resolved_at, NEW.reason, NEW.consumed_at)
                   IS DISTINCT FROM
                   (OLD.decision, OLD.resolved_by, OLD.resolved_at, OLD.reason, OLD.consumed_at)
                THEN
                    RAISE EXCEPTION 'a cancellation sets closed_at and nothing else';
                END IF;
                NEW.closed_at := db_now;
            ELSIF OLD.status = 'pending' AND NEW.status = 'expired' THEN
                IF NOT (as_requester OR as_approver) THEN
                    RAISE EXCEPTION 'only the requester or approver role may expire';
                END IF;
                IF OLD.expires_at > db_now THEN
                    RAISE EXCEPTION 'the approval has not reached its expiry';
                END IF;
                IF (NEW.decision, NEW.resolved_by, NEW.resolved_at, NEW.reason, NEW.consumed_at)
                   IS DISTINCT FROM
                   (OLD.decision, OLD.resolved_by, OLD.resolved_at, OLD.reason, OLD.consumed_at)
                THEN
                    RAISE EXCEPTION 'an expiry sets closed_at and nothing else';
                END IF;
                NEW.closed_at := OLD.expires_at;
            ELSIF OLD.status = 'approved' AND NEW.status = 'consumed' THEN
                IF NOT as_requester THEN
                    RAISE EXCEPTION 'only the requester role may consume';
                END IF;
                IF OLD.expires_at <= db_now THEN
                    RAISE EXCEPTION 'the approval has expired';
                END IF;
                IF (NEW.decision, NEW.resolved_by, NEW.resolved_at, NEW.reason, NEW.closed_at)
                   IS DISTINCT FROM
                   (OLD.decision, OLD.resolved_by, OLD.resolved_at, OLD.reason, OLD.closed_at)
                THEN
                    RAISE EXCEPTION 'a consumption sets consumed_at and nothing else';
                END IF;
                NEW.consumed_at := db_now;
            ELSE
                RAISE EXCEPTION 'an approval cannot move from % to %', OLD.status, NEW.status;
            END IF;
            RETURN NEW;
        END;
        $$;
        """  # noqa: S608 - only the two fixed role names are interpolated
    )


def downgrade() -> None:
    raise NotImplementedError(
        "core_0009 is not reversible: re-create the core_0006 guard by hand if you must"
    )
