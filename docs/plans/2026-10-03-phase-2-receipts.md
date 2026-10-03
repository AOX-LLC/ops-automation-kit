# Phase 2 — Receipts on agent-core v0.1.0a2: plan

**Goal:** receipts folder → extracted fields → deterministic reconciliation against the bank CSV → spreadsheet with mismatches flagged → summary email, scored by agent-core's eval runner, with model calls routed through agent-core v0.1.0a2.

**Cut line:** the shim, extraction with live recordings, reconciliation, the workflow and the scorecards. If time runs short, drop the approver restyle first, then the summary email.

## 1. The shim (`opskit.core`; main session)

`opskit.core.ports` re-exports agent-core's types, so the rest of the app keeps importing only `opskit.core` (import-linter still forbids `aox_agent_core` elsewhere).

| Old name in this repo | Now |
| --- | --- |
| `Mode.MOCK` / `MOCK_MODE` | `Mode.REPLAY` / `AGENT_CORE_MODE` (`replay` by default; `live` and `record` need `AGENT_CORE_ANTHROPIC_API_KEY`) |
| `Tier`, `PromptRef`, `Attachment` | agent-core's (`Attachment.from_bytes`) |
| `RunContext(run_id: UUID, workflow, mode, external_ids)` | agent-core's `RunContext(run_id=str(uuid), external_ids={"workflow", "n8n_workflow_id", "n8n_execution_id"})` |
| `ModelClient.structured(...) -> ModelResult` | agent-core's `ModelClient.call(...) -> CallResult`, wrapped by `MeteredModelClient`, which writes `core.model_calls` and a `model.call` audit record after each call |
| `ApprovalQueue.request/decide/get/list_pending/expire_due` | `PgApprovalQueue` implementing agent-core's protocol: `submit` (plus an extra `resume_url` keyword), `get`, `list_pending(principal, after=UUID)`, `resolve(principal)`, `consume`. 03 keeps `expire`, `expire_due`, `list_pending_page` (string cursor, `Page`) and the outbox. `edited_subject` is dropped. |
| `AuditLog.append(ctx, actor, …)` | `PgAuditLog` implementing agent-core's protocol: `append(AuditEvent) -> AuditRecord`, `iter_records`, `head`, `verify`. Hash-chained with agent-core's `compute_record_hash` (schema 2); appends are serialized with a transaction-scoped advisory lock. `append_in(session, event)` keeps writes inside a caller's transaction. |
| Approver identity | `Principal("approver", HUMAN, {"approver"})`. n8n is `Principal("service.n8n", SERVICE)`. `required_role = "approver"`. The policy is agent-core's `RoleApproverPolicy`, evaluated inside `resolve`. |

- **Migration `core_0003`:**
  - The approvals columns follow agent-core's names (`action`, `summary`, `payload`, `payload_sha256`, `requested_by`, `required_role`, `decision`, `resolved_by`, `resolved_at`, `consumed_at`, `reason`, `run_context`), and `edited_subject` is dropped.
  - The Phase 1 audit table is kept read-only as `core.audit_log_v1`. A new hash-chained `core.audit_log` gets the same append-only triggers and grants.
- **Approver page:** decisions post `approve` or `reject`. Expiry leaves `resolved_by` and `resolved_at` empty, as agent-core's model requires.
- **Integration tests (50):** they stay; they change only where agent-core's semantics changed, and each change is listed in the PR.

## 2. Extraction (Sonnet, against the shim's interface)

- `opskit/receipts/extraction.py`:
  - `ReceiptExtraction` (Pydantic): vendor, receipt date, currency, subtotal, tax, tip, total (integer cents, all nullable), card last4, receipt number, line items.
  - `PromptRef("receipts.extract", v1)` with the honest-null instruction.
  - `prepare_attachment(path)`.
- **Caps enforced before any call (this repo's own):**
  - images 4 MiB and 2048 px on the long edge; larger images are downscaled, with EXIF orientation applied;
  - PDFs 5 MB and at most 2 pages;
  - unreadable or oversized → `needs_review` with a reason, never sent.
- **Routing:** `routing.count_pdf_pages = false` in `config/agent-core.toml`, and `routing.tasks.extraction = "small"`.

## 3. Reconciliation rules (main session decides; Sonnet implements; no model calls)

Inputs are the extracted receipts and the bank CSV, with amounts in integer cents and debits as absolute values.

1. **Out of scope:** bank credits, and lines whose description starts with `ACH CREDIT`, `TRANSFER` or `DEPOSIT`. A positive amount that matches a refund receipt is in scope.
2. **Candidates for each receipt:** same absolute amount, or within 20 % with a merchant-token match. The posting date runs from 1 day before to 10 days after the receipt date. Merchant similarity is the token overlap between the vendor and the bank descriptor, after removing processor prefixes (`SQ *`, `TST*`, `PAYPAL *`) and digits.
3. **Assignment:** greedy, best first. The score orders by exact amount, then merchant similarity, then smallest lag. Each bank line is used once.
4. **Duplicate receipt:** same vendor, date and total as an already-matched receipt → `duplicate_receipt`, no bank line.
5. **Labels for a matched pair:**
   - amounts differ → `amount_mismatch` (this wins over date);
   - lag of 0–3 days → `matched`;
   - lag over 3 days → `date_drift`.
6. **Leftovers:**
   - a receipt with no candidate → `missing_in_bank`;
   - a bank debit whose descriptor and amount equal an earlier matched line within 7 days → `duplicate_charge`;
   - any other unused debit → `unreceipted_charge`.
7. Every row carries `delta_cents` and `delta_days` as the answer key defines them. A receipt flagged `needs_review` in extraction is reported as such and never matched.

## 4. n8n workflow (Sonnet)

```
Schedule
  → GET /v1/receipts/pending
  → Split Out
  → POST /v1/receipts/extract (one receipt each)
  → Aggregate
  → POST /v1/receipts/reconcile
  → Convert to File (XLSX, one Reconciliation sheet; needs-review rows are in it with their status)
  → Read/Write Files from Disk (exports/)
  → IF there are flags
  → Send Email (summary to Mailpit)
```

No Code nodes. Exported to `n8n/workflows/01-receipts.json` and published on boot. A header-authenticated webhook trigger sits beside the schedule, so the smoke test can run it on demand.

## 5. Evals (main session designs; Sonnet wires)

- **Extraction:** an `EvalSuite` of the 30 receipts with the answer key as `expected`. The custom target calls the extraction service.
  - One scorer per field (vendor, date, subtotal, tax, tip, total, last4), using exact comparison after normalization.
  - The field-level accuracy table is computed from the results.
  - Cost per receipt and latency come from the live recording run, which is committed as its own scorecard.
- **Reconciliation:** run on the answer key's true receipt fields, so this measures the matcher, not extraction. Also run end to end on the extracted fields.
  - Precision and recall per flag type.
- **Outputs:** `evals/scorecards/receipts-*.json` and `.md`. CI replays them and fails if accuracy drops below the committed floor.

## 6. Live recording (main session runs it)

- `AGENT_CORE_MODE=record` on the small tier, all 30 receipts, cost summed from `CallResult.cost_usd`, with a hard stop at $2.00.
- If small-tier field accuracy is below 0.9, also record the mid tier and report both.
- Only a2-format recordings are committed, under `fixtures/cassettes/`. The hand-written smoke fixture moves to the a2 format; it never scores extraction.

## 7. Hub fixes

- `mem_limit` on every Compose service, one-shots included.
- Rebase Dependabot PRs #2 and #3 and let CI run. Don't merge them.

## Delegation

| Owner | Work |
| --- | --- |
| Main session | shim, migration, approvals and audit backends, integration-test updates, reconciliation rules, eval design, the recording run, review of every diff |
| Sonnet | extraction plumbing, reconciliation code, workflow JSON, eval wiring, fixtures, Compose changes |

## As built (2026-10-03)

- **Matches the plan:** the shim, extraction, reconciliation, the workflow, the evals and the hub fixes.
- **Differences:**
  - The image cap is 2048 px, not 4096 px.
  - The spreadsheet has one sheet.
  - The receipts workflow is published, not left unpublished.
  - `routing.budget_usd_per_call` is $1.00: with `count_pdf_pages = false`, a PDF is budgeted at 100 pages ($0.88 on the small tier).
- **Reconciliation:** the rules are refined in `src/opskit/receipts/reconcile.py` and described in `docs/architecture.md` §A10.
- **Small-tier recording:** 30 of 30 receipts, mean field accuracy 1.0, $0.0028 per receipt, $0.15 of total recording spend.
- **Mid tier:** not recorded, because accuracy was not poor.
