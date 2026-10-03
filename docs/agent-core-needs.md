# What this repo needs from agent-core v0.1.0

Status (Phase 2): pinned to v0.1.0a2; items (a) are met; the kit keeps items (b) in its own adapter (src/opskit/core/pg/).

The ops kit drives runs from n8n, so they are long-lived, cross-process, and resumed over HTTP, not by in-process callbacks. Agent-core gates five projects, so list (a) is kept to what cannot be bridged here without forking agent-core. Everything in (b) lives in `opskit/core/` until a later agent-core release absorbs it.

## (a) Must be in v0.1.0

1. **Stable protocols with pluggable backends.**
   - Public `ModelClient`, `ApprovalQueue`, `AuditLog` and `RunContext` types.
   - A host app can pass in its own `ApprovalQueue` and `AuditLog` implementations; this repo supplies Postgres ones.
   - `RunContext` carries `run_id` plus arbitrary `external_ids` (n8n workflow and execution IDs) into audit calls and trace spans.
   - Draft signatures: [`docs/architecture.md` §A9](architecture.md).
2. **Content-addressed replay keys with strict replay.**
   - The key is derived only from prompt ID and version, tier, schema name, normalized inputs and attachment sha256s.
   - It never includes run IDs, execution IDs, timestamps, call order or the model ID.
   - The key function is public.
   - A replay miss raises an error naming the key and expected path, with no live fallback. The fixture root is configurable per project.
3. **Image and PDF attachments on structured-output calls.** PNG, JPEG and PDF go in as attachments on a Pydantic-schema call. Attachments enter the replay key by hash, and fixtures never store their bytes.
4. **Key and config conventions, already decided.** Read `AGENT_CORE_ANTHROPIC_API_KEY` only, keep tiers in config, ship typed (`py.typed`), support Python 3.12, have no import-time side effects, and install from a git tag.

## (b) Lives in this repo's adapter until a later agent-core release

5. Postgres `ApprovalQueue` and `AuditLog` backends: async, using a caller-provided session, with a configurable schema.
6. Migrations runnable by the host without superuser, plus grant-friendly roles and DB-enforced append-only audit.
7. External resume targets on approvals, stored but never listed or audited, with the resume enqueued in the decision's transaction (outbox) and an at-least-once dispatcher.
8. Decision semantics: conditional `decide()` (`ApprovalNotPending`, `ApprovalExpired`), an `expire_due(now)` sweep, `edited_subject`, the recorded `actor`, and keyset-paginated `list_pending`.
9. Image preprocessing before the call: EXIF orientation, max long edge and byte cap.
10. An "honest nulls" convention: a schema helper or prompt pattern for "not present → null" rather than a guessed value.
11. Atomic record mode and a tool that lists missing or stale fixtures.
12. **Tag timing** (done: pinned to v0.1.0a2 in Phase 2). Phase 2 pins `v0.1.0a1` as soon as agent-core tags it, then moves to `v0.1.0`. Where agent-core's names differ from §A9, the shim lives in `opskit/core/factory.py`.
