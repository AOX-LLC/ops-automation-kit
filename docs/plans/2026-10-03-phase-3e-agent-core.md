# Phase 3e: agent-core v0.1.0a6

Target: **v0.1.0a6**. v0.1.0 is not tagged, a5 was tagged but never released (its CHANGELOG says so),
so the newest usable tag is a6. The recording format stays 2: `uv lock --upgrade-package aox-agent-core`
moved only that package; `anthropic` and `pydantic` are unchanged, so no recording is stale and
nothing is re-recorded. Read: agent-core `docs/compat-03.md`, `docs/upgrading.md`, `CHANGELOG.md` at a6.

The kit keeps its own `PgAuditLog` and `PgApprovalQueue` (compat-03 row 5), so this phase ports
what a4 to a6 added to *those two classes and their tables*; it does not adopt agent-core's SQL
backends or installer.

## Scope

Everything is a database rule first and code second. Every 3d trigger and bound stays; each one is
extended for the new column or transition, never loosened.

### 1. Pin
Done in the first commit. Docs that name the version follow it.

### 2. Audit (`core_0011`)
- `append_many(events)`: validates and scans every event first (an error names the event's index),
  takes the chain lock once, writes consecutive records in order in one transaction, returns them.
  Empty writes nothing; more than agent-core's `MAX_APPEND_BATCH` (1000) is a `ValueError`.
  `append` is `append_many([event])`.
- `occurred_at`: `None` means the database clock (read inside the lock). A supplied value must lie
  within `OCCURRED_AT_MAX_PAST` (24 h) before and `OCCURRED_AT_MAX_FUTURE` (5 min) after the
  database clock, else `AuditTimeRejectedError`; the insert trigger enforces the same bounds from
  `statement_timestamp()` so plain SQL cannot go around the code. 3d left these bounds open on purpose.
- `recorded_at timestamptz` (nullable: rows from before have none), set by the insert trigger from
  the database clock whatever was sent, like `db_role`. Neither is in the hash; `occurred_at` still is.
  Fit with 3d: the trigger is the existing `audit_log_set_db_role` function replaced (still
  `ENABLE ALWAYS`); `audit_log_bounds` is untouched; the table stays append-only.

### 3. Approvals (`core_0012`, then code)
- **Delegate rule** (own commit): the guard refuses `resolved_by` in `OLD.delegates` on a decision,
  beside the existing requester check; the library policy already denies it
  (`DenialReason.DELEGATE_APPROVAL`). `list_pending` no longer offers a request to its own delegate.
- **EXPIRED may carry an approve decision**: `approved -> expired` is allowed to either role once
  `expires_at` has passed (database clock), `closed_at := OLD.expires_at` (the same rule 3d fixed for
  pending expiry), decision fields unchanged. Readers report an approved request past its lifetime as
  EXPIRED. The sweep closes approved rows too (audited, with `previous_status`). The old decision
  CHECK constraints are relaxed for exactly that pair and no other.
- **`payload_purged_at` and `purge_payloads`**: `payload` becomes nullable, tied by a CHECK to
  `payload_purged_at` (exactly one of the two is set). The guard allows one change to them: by the
  approver role only, on a *finished* row (consumed, rejected, cancelled, expired), payload non-NULL to
  NULL with `payload_purged_at` NULL to non-NULL, when the finish time plus the **24 h retention floor**
  is already past by the database clock; `payload_purged_at := statement_timestamp()` whatever the
  statement carried. The finish times it counts from are the ones 3d made database-set
  (`consumed_at`, a rejection's `resolved_at`, `closed_at`), so a caller cannot backdate one to slip
  under the floor. The approver role gets `UPDATE (payload, payload_purged_at)`; the requester gets
  neither. `purge_payloads(*, principal, older_than, limit=500) -> int` runs on the approver
  connection, refuses `older_than` below the floor (`ValueError`), selects due rows
  `FOR UPDATE SKIP LOCKED`, stamps them, and writes one `approval.payload_purged` audit record each,
  in one batched append at the end of the transaction. It is not scheduled in this phase: nothing deletes data on a timer until a
  retention period is chosen (listed under follow-ups).
- **Idempotent submit**: partial unique index `approvals_one_open` on
  `(requested_by, action, payload_sha256) WHERE status IN ('pending','approved')`. `submit` closes an
  open request of that key that has lapsed, then looks for an open one: an exact repeat (same
  `summary`, `required_role`, lifetime `expires_at - created_at == ttl_seconds`, `delegates`) returns it
  and writes no audit record; any difference raises `ApprovalConflictError(existing, differs)` audited as
  `approval.submit_conflict`. A race lands on the index; the loser re-reads. `context` is not compared
  (as in agent-core). **`resume_url` is compared** (added after review; agent-core has no push delivery,
  the kit's resume URL is its delivery channel): a retried n8n execution has a new URL, and handing it
  the old approval would resume the dead execution while the human's decision was discarded. An HTTP
  retry inside one execution keeps its URL, so it stays idempotent. The migration turns existing duplicates into a report:
  lapsed open rows (3d left unused approvals `approved` for ever) are expired first; of the live ones
  each group keeps its approved request (else its oldest) and the other pending ones are cancelled,
  with the guard off for those statements, as `core_0010` did; two live approved in one group stops
  the migration with the ids and says how to resolve it. The HTTP contract is unchanged: a repeat gets the existing request's id and
  status back; a conflict is a 409 with the existing id.
- **Payload verification**: the kit always stores the payload (the approver must see what the hash binds).
  `include_payload` is accepted for protocol compatibility and controls only whether the returned
  `ApprovalRequest.payload` is filled. Every read of the stored payload (`payload_of`, `get`, the
  pending listing, the decision) recomputes the hash against `payload_sha256`: `get` and `payload_of`
  raise `ApprovalIntegrityError`, the listing omits the row (recorded once), the decision is refused
  and audited (`payload_mismatch`, as today). Withdrawing, consuming and expiring never check, so a
  requester can always close a request. A purged payload reads as purged, not as a mismatch.
- Approver-not-requester-or-delegate in the trigger as well as in code: see the delegate rule.

### 4. `wait_for_decision`
**Not in a6.** agent-core's CHANGELOG lists it, with concurrent-append coalescing, the `anthropic`
extra and a pgbouncer job, as planned for 0.1.0a7. There is nothing to adopt, so the n8n wait,
the HTTP contract and the workflows are untouched.

### 5. Leads
Cap after Unicode normalisation (normalise, then measure), with a test. Delegated to a Sonnet subagent.

### 6. THIRD_PARTY_NOTICES.md
Added if missing, linked from the README's License section. Delegated to a Sonnet subagent.

### 7. Tests
Integration tests for each item above, in particular: an n8n retry of the same draft approval returns
the existing request; a delegate's approval is refused through direct SQL; purge cannot be triggered
early by a backdated time; `occurred_at` outside the window is refused.

## Order and commits
pin (done) → plan → audit migration + code → delegate rule → expired-approved → idempotent submit →
payload verification → purge → leads → notices → docs. One commit per concern; refactors separate.

## Not in scope, noted
- Scheduling `purge_payloads` (needs a retention decision).
- agent-core's DB-side chain check (`seq` and `prev_hash` in the insert trigger): the kit still
  serialises appends with the advisory lock and verifies the chain on read.
- `connection=` host transactions, the sync facades, SQLite: the kit uses none of them.
