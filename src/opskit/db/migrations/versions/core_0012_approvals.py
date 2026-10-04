"""core: what agent-core v0.1.0 added to approvals, enforced by the database.

Four rules, one replaced guard (`core.approvals_guard`, as `core_0009` left it, with these changes
only):

1. **One open request per requester, action and payload hash.** A partial unique index on
   `(requested_by, action, payload_sha256)` where the status is `pending` or `approved`. It holds
   when calls race; the queue's `submit` turns an exact repeat into the existing request.
   Duplicates already stored are closed first and reported (below).
2. **An approver is neither the requester nor a listed delegate.** The guard refuses a
   `resolved_by` that is in the request's `delegates`, beside the requester check it had, so plain
   SQL cannot go around the policy in code. Ids are compared exactly.
3. **`approved -> expired`.** An approval that lapses unused must close, or it would hold the
   unique key for ever. Either role may do it once `expires_at` has passed by the database clock;
   `closed_at` is the row's own `expires_at`, as for a pending expiry (3d), and the decision fields
   stay. `approvals_decision_matches_status` allows `expired` with `approve` and nothing else new.
4. **Purging a stored payload.** `payload` is nullable and tied by a CHECK to the new
   `payload_purged_at` (exactly one of them is set). The guard allows one change to them: the
   approver role sets `payload` to NULL on a finished request (consumed, rejected, cancelled or
   expired) whose finish time plus the retention floor, 24 hours, is past by the database clock.
   `payload_purged_at` is then the database clock, whatever the statement carried. The finish times
   are the ones 3d made database-set, so none can be backdated to slip under the floor. Only the
   approver role gets UPDATE on the two columns. `payload_sha256` never changes, so what the
   payload was stays bound.

Every 3d bound stays: the guard still stamps `created_at`, `expires_at`, `resolved_at`, `closed_at`
and `consumed_at` itself, and `approvals_bounds` is re-attached with the one change that `payload`
may be NULL (only a purge makes it so; the CHECK and the guard refuse any other NULL).

Duplicates. Under 3d an approved request that was never used stayed `approved` for ever, so a
long-lived database holds lapsed open rows. Before the index is built they are expired in place
(guard off for that statement, `closed_at` their own `expires_at`) and reported. Of the requests
still live, each group with the same key keeps its approved request (else its oldest) and the
others, which are pending, are cancelled and reported. No audit record is written for either. A
group with two live approved requests stops the migration with the ids: a person decides which one
stands, as the table owner with the guard off (the error gives the statements).

Revision ID: core_0012
Revises: core_0011
"""

from alembic import op

from opskit.db import bounds as b

revision = "core_0012"
down_revision = "core_0011"
branch_labels = None
depends_on = None

REQUESTER = "opskit_app"
APPROVER = "opskit_approver"
# The shortest age a finished request's payload may be purged at, as the guard enforces it.
RETENTION_FLOOR_SECONDS = 24 * 3600

# core_0010's approvals spec with one change: `payload` may be NULL once purged.
APPROVALS = {
    "payload": b.document("object", max_bytes=131072, depth=8, nullable=True),
    "resume_url": b.text(512, regex=r"^https?://[^[:space:]]+$", nullable=True),
    "run_context": b.document("object", max_bytes=2048, depth=3, nullable=True),
    "run_context.external_ids": b.id_map(
        16,
        key_regex=r"^[a-z][a-z0-9_]{0,63}$",
        value_regex=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$",
        nullable=True,
    ),
    "delegates": b.document("array", max_bytes=4096, depth=2, items=16),
}

CLOSE_DUPLICATES = """
    ALTER TABLE core.approvals DISABLE TRIGGER approvals_guard;
    DO $dups$
    DECLARE
        two_approved uuid[];
        cancelled uuid[];
        lapsed uuid[];
    BEGIN
        -- A lapsed open request must not hold a key, or be kept over a live one below.
        WITH done AS (
            UPDATE core.approvals SET status = 'expired', closed_at = expires_at
            WHERE status IN ('pending', 'approved') AND expires_at <= statement_timestamp()
            RETURNING id)
        SELECT coalesce(array_agg(id), '{}') INTO lapsed FROM done;
        RAISE WARNING 'approvals: % lapsed open request(s) expired: %',
            cardinality(lapsed), lapsed;
        SELECT array_agg(id) INTO two_approved FROM (
            SELECT id, count(*) OVER (PARTITION BY requested_by, action, payload_sha256) AS n
            FROM core.approvals WHERE status = 'approved') a WHERE n > 1;
        IF two_approved IS NOT NULL THEN
            RAISE EXCEPTION 'live approved requests share a requester, action and payload: %. '
                'As the table owner run: ALTER TABLE core.approvals DISABLE TRIGGER '
                'approvals_guard; UPDATE core.approvals SET status = ''cancelled'', '
                'closed_at = now(), decision = NULL, resolved_by = NULL, resolved_at = NULL '
                'WHERE id = <the id that should not stand>; ALTER TABLE core.approvals '
                'ENABLE ALWAYS TRIGGER approvals_guard; then run the migration again.',
                two_approved;
        END IF;
        WITH ranked AS (
            SELECT id, status,
                   row_number() OVER (
                       PARTITION BY requested_by, action, payload_sha256
                       ORDER BY (status = 'approved') DESC, created_at, id) AS place,
                   count(*) OVER (PARTITION BY requested_by, action, payload_sha256) AS n
            FROM core.approvals WHERE status IN ('pending', 'approved')),
        closed AS (
            UPDATE core.approvals a
            SET status = 'cancelled', closed_at = now()
            FROM ranked r
            WHERE a.id = r.id AND r.n > 1 AND r.place > 1 AND a.status = 'pending'
            RETURNING a.id)
        SELECT coalesce(array_agg(id), '{}') INTO cancelled FROM closed;
        RAISE WARNING 'approvals: % duplicate open request(s) cancelled: %',
            cardinality(cancelled), cancelled;
    END
    $dups$;
"""


def upgrade() -> None:
    # Nothing may change an approval while the duplicates are decided and the guard is replaced.
    op.execute("LOCK TABLE core.approvals IN EXCLUSIVE MODE")
    op.execute(
        """
        ALTER TABLE core.approvals ADD COLUMN payload_purged_at timestamptz;
        ALTER TABLE core.approvals ALTER COLUMN payload DROP NOT NULL;
        ALTER TABLE core.approvals ADD CONSTRAINT approvals_payload_purge_matches CHECK (
            (payload IS NULL) = (payload_purged_at IS NOT NULL));
        ALTER TABLE core.approvals DROP CONSTRAINT approvals_decision_matches_status;
        ALTER TABLE core.approvals ADD CONSTRAINT approvals_decision_matches_status CHECK (
            (status IN ('approved', 'consumed') AND decision IS NOT DISTINCT FROM 'approve')
            OR (status = 'rejected' AND decision IS NOT DISTINCT FROM 'reject')
            OR (status = 'expired' AND (decision IS NULL OR decision = 'approve'))
            OR (status IN ('pending', 'cancelled') AND decision IS NULL));
        """
    )
    op.execute(CLOSE_DUPLICATES)
    op.execute(
        f"""
        CREATE UNIQUE INDEX approvals_one_open
            ON core.approvals (requested_by, action, payload_sha256)
            WHERE status IN ('pending', 'approved');

        -- The decision side purges; the requester side has no grant on either column.
        GRANT UPDATE (payload, payload_purged_at) ON core.approvals TO {APPROVER};

        CREATE OR REPLACE FUNCTION core.approvals_guard() RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = pg_catalog, pg_temp
        AS $$
        -- payload retention floor {RETENTION_FLOOR_SECONDS} seconds
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
            is_purge boolean;
            finished_at timestamptz;
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
                -- approvals_bounds lets payload be NULL, for a purge; a new row never is, and
                -- a JSON null is not an object either.
                IF jsonb_typeof(NEW.payload) IS DISTINCT FROM 'object'
                   OR NEW.payload_purged_at IS NOT NULL THEN
                    RAISE EXCEPTION 'a new approval stores its payload, a JSON object';
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

            -- UPDATE: what a request is never changes. The stored payload is the one exception,
            -- and only by being purged (below).
            IF (NEW.id, NEW.run_id, NEW.action, NEW.summary, NEW.payload_sha256,
                NEW.requested_by, NEW.required_role, NEW.resume_url, NEW.created_at,
                NEW.expires_at, NEW.run_context, NEW.delegates)
               IS DISTINCT FROM
               (OLD.id, OLD.run_id, OLD.action, OLD.summary, OLD.payload_sha256,
                OLD.requested_by, OLD.required_role, OLD.resume_url, OLD.created_at,
                OLD.expires_at, OLD.run_context, OLD.delegates) THEN
                RAISE EXCEPTION 'identity, lifetime and delegates are fixed';
            END IF;
            is_purge := OLD.payload IS NOT NULL AND NEW.payload IS NULL;
            IF (NEW.payload IS DISTINCT FROM OLD.payload
                OR NEW.payload_purged_at IS DISTINCT FROM OLD.payload_purged_at)
               AND NOT is_purge THEN
                RAISE EXCEPTION 'a stored payload changes only by being purged';
            END IF;

            IF is_purge THEN
                IF NOT as_approver THEN
                    RAISE EXCEPTION 'only the approver role may purge a stored payload';
                END IF;
                IF OLD.status NOT IN ('consumed', 'rejected', 'cancelled', 'expired') THEN
                    RAISE EXCEPTION 'only the payload of a finished request may be purged';
                END IF;
                -- The finish times are database-set, so the caller cannot move them.
                finished_at := CASE OLD.status WHEN 'consumed' THEN OLD.consumed_at
                                               WHEN 'rejected' THEN OLD.resolved_at
                                               ELSE OLD.closed_at END;
                IF finished_at IS NULL
                   OR extract(epoch FROM finished_at) + {RETENTION_FLOOR_SECONDS}
                      > extract(epoch FROM db_now) THEN
                    RAISE EXCEPTION 'a payload may be purged only after the retention floor';
                END IF;
                IF NEW.payload_purged_at IS NULL OR OLD.payload_purged_at IS NOT NULL
                   OR NEW.status IS DISTINCT FROM OLD.status
                   OR (NEW.decision, NEW.resolved_by, NEW.resolved_at, NEW.reason,
                       NEW.consumed_at, NEW.closed_at)
                      IS DISTINCT FROM
                      (OLD.decision, OLD.resolved_by, OLD.resolved_at, OLD.reason,
                       OLD.consumed_at, OLD.closed_at) THEN
                    RAISE EXCEPTION 'a purge sets payload to NULL and payload_purged_at only';
                END IF;
                NEW.payload_purged_at := db_now;
                RETURN NEW;
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
                -- The approver is neither the requester (above) nor a delegate, which plain SQL
                -- could otherwise be used to get around.
                IF jsonb_exists(OLD.delegates, NEW.resolved_by) THEN
                    RAISE EXCEPTION 'a delegate of the request may not decide it';
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
            ELSIF OLD.status IN ('pending', 'approved') AND NEW.status = 'expired' THEN
                -- An approval that lapses unused expires too, or it would hold the one-open
                -- index for ever. Its decision, approver and times stay as they were.
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

        DROP TRIGGER approvals_bounds ON core.approvals;
        {b.attach_sql("core.approvals", "approvals_bounds", APPROVALS)}
        ALTER TABLE core.approvals ENABLE ALWAYS TRIGGER approvals_guard;
        """  # noqa: S608 - only fixed names and numbers are interpolated
    )


def downgrade() -> None:
    raise NotImplementedError(
        "core_0012 is not reversible: closed duplicates and purged payloads cannot be restored"
    )
