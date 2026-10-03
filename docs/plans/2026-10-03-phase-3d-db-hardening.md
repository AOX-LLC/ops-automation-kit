# Phase 3d: database hardening

Scope is the five items in the roadmap's 3d row: F3, F6, F7, the expiry-test fix, and bounds plus
reader resilience for requester-writable columns. Pinned to agent-core v0.1.0a3; the a4 upgrade is
Phase 3e. agent-core's `tests/test_untrusted_columns.py` (v0.1.0a4) is used for its pattern only.

## Design rules

1. **Every bound is enforced in the database, on write.** Rows already stored are never rewritten and
   never block an unrelated update (see "Existing rows").
2. **A reader that meets a row it cannot parse fails closed for that row only.** It skips or refuses
   the row, audits it once, and carries on for everyone else.
3. **Nothing is bounded tighter than the application already is,** except where the application has no
   bound at all. Values the application legitimately writes are listed in "Allowed on purpose".

## 1. F3: approver sessions

- `core_0007` adds `core.approver_sessions_guard` (BEFORE INSERT OR UPDATE, enabled always):
  `revoked_at` may go only from NULL to non-NULL, once, and is set from `statement_timestamp()`;
  nothing else in the row changes after insert; `created_at` is set by the database.
- The requester role loses **all** access to the table. `SessionStore` moves to the approver
  connection (`make_session_factory(approver_engine)`), which gets `SELECT, INSERT, UPDATE (revoked_at)`.
  No requester path needs `csrf_token`, so no function or narrower grant is needed: the whole store
  was on the wrong side. `/healthz` and the sweep do not touch the table.
- Insert bounds: `csrf_token` matches `^[A-Za-z0-9_-]{16,128}$`; `expires_at` is after the database
  clock and at most 30 days ahead. `Settings.approver_session_hours` gets `1..720` so the setting
  cannot exceed the bound.

## 2. F6: outbox

`core.outbox_guard` (BEFORE INSERT, enabled always) refuses an insert unless: the caller is the
approver role; the approval exists; its stored status is `approved` for decision `approve` and
`rejected` for `reject`; the payload is exactly `{"approval_id": <that id>, "decision": ...}`;
`delivered_at` is NULL and `attempts` is 0. It sees the status update made earlier in the same
transaction, so the decision path is unchanged. A requester-side UPDATE guard keeps `attempts` in
`0..10`, `last_error` to 200 characters, `next_attempt_at` within an hour of the database clock, and
`delivered_at` NULL to non-NULL only, set from `statement_timestamp()`.

## 3. F7: the guard owns the clock

In `approvals_guard`, caller-supplied times are ignored and replaced:

| Event | Set to |
| --- | --- |
| insert | `created_at := statement_timestamp()`; `expires_at := created_at + (caller's expires_at - caller's created_at)`; the caller's lifetime must be `> 0` and `<= 604800` s. The old "created_at within 5 minutes" check goes: the caller's `created_at` is no longer trusted at all |
| approve or reject | `resolved_at := statement_timestamp()` |
| consume | `consumed_at := statement_timestamp()` |
| cancel | `closed_at := statement_timestamp()` |
| expire | `closed_at := OLD.expires_at` |

**Decision for the review.** Expiry keeps `closed_at = expires_at`. That value comes from the stored row,
not the caller, and a3's contract plus our queue docs and tests pin it ("either way `closed_at` is its
`expires_at`"; readers report a due pending row that way before the sweep stores it). Using
`statement_timestamp()` there would make a row read differently before and after the sweep. Everything
the caller could influence is database-set.

## 4. Expiry test

Replace `assert out["swept"] >= 0` with a check of the stored row through `psql`: status `expired`,
`closed_at = expires_at`, exactly one `approval.expired` audit record, whichever sweeper got there.
No assertion on `swept`.

## 5. Untrusted columns

### 5a. Mechanism

One function, `core.enforce_bounds()` (BEFORE INSERT OR UPDATE, enabled always), driven by a JSON spec
passed as the trigger argument. Kinds: `text` (min, max characters; octet length is also capped at
4x), `regex`, `enum`, `json` (type, max bytes, max depth), `int`/`numeric` (min, max), `time` (range
relative to the database clock). It checks a column on insert, and on update **only if that column
changed**. Depth is measured with `jsonb_path_exists(v, '$.**{N to last}')`, which is not recursive.
`core.approvals` and `core.audit_log` extend their existing guards instead. A function
`core.bounds_violations(table, spec)` runs the same checks over stored rows for the migration report
and for tests. Each domain branch adds a revision (`crm_0003`, `receipts_0003`, `leads_0003`,
`inbox_0003`) with `depends_on = core_0007`.

### 5b. Requester-writable columns and bounds

"Requester-writable" is every column `opskit_app` can INSERT or UPDATE (grants listed in the
inventory). Columns the database already constrains (status/mode/route/tier/workflow enums, the
approvals shapes in `core_0006`) are kept as they are.

| Table.column | Bound |
| --- | --- |
| core.runs `n8n_workflow_id`, `n8n_execution_id` | NULL or `^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$` (agent-core's id pattern; the API's own cap is 64). A looser value makes `RunContext` fail on every approval submit |
| core.runs `finished_at` | not before `started_at`; at most 5 min ahead of the database clock |
| core.approvals `payload` | object, at most 32 KiB, depth 8 (inbox replies are about 1.1 KB) |
| core.approvals `resume_url` | NULL or at most 512 characters, `^https?://[^[:space:]]+$` |
| core.approvals `run_context` | at most 2048 bytes; `external_ids` an object of at most 16 keys; keys and string values match the id pattern |
| core.approvals `delegates` | at most 4096 bytes (array and element shape already checked) |
| core.approvals `created_at`, `expires_at`, `resolved_at`, `closed_at`, `consumed_at` | database-set (section 3); lifetime at most 7 days |
| core.outbox `payload`, `attempts`, `next_attempt_at`, `delivered_at`, `last_error` | section 2 |
| core.audit_log `action`, `actor_id`, `subject_id` | action at most 100 and `^[a-z][a-z0-9_]*([.][a-z][a-z0-9_]*)*$`; actor `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`; subject NULL or `^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$` |
| core.audit_log `payload`, `run_context` | payload: text that parses as a JSON object, at most 8192 bytes; run_context: NULL or a JSON object at most 2048 bytes. Same limits as agent-core's own `AuditEvent` |
| core.audit_log `occurred_at` | **not touched**; its bounds and `recorded_at` are Phase 3e |
| core.model_calls | `prompt_id` `^[a-z][a-z0-9_.-]{0,99}$` or `adhoc`; `prompt_version` 0..10000; `model` at most 100; `replay_key` 64 hex; tokens 0..10^9; `cost_usd` 0..10000; `latency_ms` 0..10^8 |
| core.sample_files | `path` 1..512, no leading `/`, no `..`; `workflow` and `kind` enums (the 3 and 7 values the seed writes); `sha256` 64 hex; `bytes` at least 0 |
| core.approver_sessions | section 1 |
| inbox.messages | `message_id` 1..998; `mailpit_id` 1..200; `from_header` at most 1000; `reply_to_header`, `to_addr` at most 2000; `subject` at most 2000; `received_at` 1990..2100; `body_text` at most 1 MiB |
| inbox.triage | `category` 10 values, `priority` 4 values; `injection_reasons` array of at most 16, 16 KiB, depth 3; `replay_key` 64 hex; metrics as model_calls |
| inbox.drafts | `to_addr` at most 320; `subject`, `in_reply_to` at most 998; `body` at most 64 KiB; `facts_used` array at most 64 items, 16 KiB, depth 2; `grounding` object, 16 KiB, depth 3; `failure_reason` at most 200; `sent_at` within a day of the database clock; metrics |
| leads.research | `company_name` 1..200; `city_hint` at most 100; `website`, `domain` at most 253; `reason` at most 300; `fields` object 16 KiB depth 4; `findings` array of at most 256, 64 KiB; `pages` array of at most 64, 16 KiB; cites 0..10000; metrics |
| crm.accounts | `name` 1..200; `domain` at most 253; `industry`, `hq_city` at most 200; `employee_band` at most 32; `description` at most 1000; `founded_year` 1800..2100 |
| crm.account_sources | `field` the 6 field names; `source_ref` at most 512; `excerpt` at most 1000 |
| receipts.extractions | `path` 1..512; `sha256` empty or 64 hex; `reason` at most 200; `fields` NULL or object 64 KiB depth 4; `tier`, `model` at most 100; metrics |
| receipts.reconciliations | `rows` array 4 MiB depth 4 (the sample set is 15.5 KB); `summary` object 4 KiB depth 2 |

Caps are wider than the largest sampled value by at least an order of magnitude, except where the
application has a cap (then they match it).

**Allowed on purpose** (the application writes these, so no bound may refuse them): failed drafts with
empty `to_addr`, `subject`, `body`; receipt rows with `sha256 = ''` and NULL `fields`;
`prompt_version = 0` and `prompt_id = 'adhoc'`; `city_hint = ''`; NULL `website` and `domain`;
`crm` seed rows from CSV.

**Application changes the bounds force.** Mail is untrusted input: a Date header of year 2150 or a
3 MiB body would now be refused by the database and fail the whole fetch. `inbox.store` therefore
clamps what it stores from Mailpit to the same limits (truncate text, NULL an out-of-range
`received_at`) before the insert. This is the only ingestion change.

### 5c. What a reader does with a row it cannot parse

| Reader | Behaviour |
| --- | --- |
| approver listing (`list_pending`, `list_pending_page`) | skips the row (already so), and now audits `approval.unreadable` once per row. Paging keeps counting fetched rows |
| `get`, `payload_of`, detail page, `cancel`, `consume`, `close_pending` | raise a new `ApprovalUnreadableError`, audited once; the routes answer 409 with a plain message, never 500. A non-object payload counts as unreadable |
| `resolve` | **refuses** an unreadable row (fails closed, nothing approved) and audits once |
| expiry sweep | per-row transactions; a poison row's context is dropped from its audit record (`context: unreadable` in the payload) but the expiry is still stored and audited. A row that still fails is logged, added to an in-process skip set and excluded from later selects, so it cannot stall the batch or the loop |
| inbox reply view | any parse failure returns None, so the page shows the generic JSON view |
| outbox worker | already tolerant (plain dataclass; `_send` catches everything; 10 attempts then excluded); no change |

"Audited once" is an `append_once_in` helper: under the chain's advisory lock it checks for an
existing record with the same action and subject, then inserts. No stored record of this kind is a
poison row's second audit.

Not changed, because they are not approver-side readers or the sweep: requester-side reads such as
`inbox.summary`, receipts `all_outcomes`, leads `_research_of`, and the audit `verify()` (no runtime
caller). The bounds above keep hostile values out of those tables going forward; their readers are not
hardened here and that is stated in the PR.

## 6. Existing rows that already exceed a bound

The migration never rewrites a stored row and never fails the upgrade on one.

- Bounds are triggers that check inserts and **changed columns only**, so a grandfathered row keeps
  working for unrelated updates (a CHECK ... NOT VALID would reject them).
- `core.audit_log` is append-only and not touched: its new insert bounds apply to new rows.
- Each migration first runs `bounds_violations` and raises a WARNING per table with the count and the
  primary keys (never the values; this repo is public and the values may be client data).
- Pending or approved-and-unconsumed approvals that violate a new bound are **cancelled** with reason
  `closed by the 3d bounds: stored value outside the new limits`, as 3c did for approved rows. The
  migration takes the table lock, disables `approvals_guard` for that one statement inside the same
  transaction, and re-enables it as ALWAYS before commit (the owner is otherwise refused by the guard).
  The ids are in the warning. They stay unreadable-safe either way (5c).
- Finished approvals and all other tables' rows stay as they are; the readers' behaviour covers them.

## 7. The inbox per-draft release flow

No route, workflow or `inbox_release` change. Indirect effects, each tested by the existing
`test_inbox_drafts`, the inbox smoke and the approval suites:

- `consumed_at` is now written by the guard, a few milliseconds off the application's value. Nothing
  reads it.
- Insert rebases `created_at` and keeps the 72-hour lifetime exactly, so the n8n Wait (72 h) and
  `expires_at` still line up to within the transaction.
- The workflow's "Check recorded decision" and `close` path now get a clean error instead of a 500 for
  an unreadable approval.
- `inbox.drafts` bounds permit the empty failed-draft values; `inbox.messages` ingestion is clamped (5b).

## 8. Order of work, one commit per concern

1. Expiry test fix (test only).
2. F3 (trigger, grant move, store wiring, settings bound) with its tests.
3. F6 with its tests.
4. F7 with its tests.
5. `enforce_bounds` and the core-table bounds, with the migration report.
6. Domain-table bounds (four migrations).
7. Inbox ingestion clamp.
8. Reader resilience (queue, routes, sweep, view, `append_once_in`).
9. Hostile-row integration suite (ported cases: deep nesting, oversized JSON, hostile session time
   zones, malformed payloads, secret-shaped names, duplicate and lone-surrogate JSON, boundary values).
10. Architecture doc.

Opus writes and reviews the guards, trigger SQL, grants and reader code. Sonnet writes the domain
revisions' specs, the clamp, the test suite and docs from this plan. No refactors are mixed in.

## Not verified by design

Requester-side readers listed in 5c; arm64/Windows; CVE scans; the owner and a superuser remain
trusted; the api container still holds both database passwords.
