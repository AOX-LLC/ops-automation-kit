# Architecture

How the kit is put together, as decided in Phase 1. The phase plan in `docs/plans/` has the task breakdown; this page is the part that stays true after the build.

## A1. Who does what

| Concern | n8n | Helper API |
| --- | --- | --- |
| Triggers and schedules | Schedule, Webhook, Manual triggers | — |
| Fan-out, branching | Split Out, Loop, IF, Switch | — |
| Waiting for a human | Wait node (resume on webhook call) | Stores the approval, serves the approver page, resumes n8n |
| Sending approved email | Send Email node over SMTP (Mailpit) | Re-checks the approval before handing over the draft |
| Spreadsheet output | Convert to File and Read/Write Files from Disk nodes (Phase 2) | Produces the reconciled rows; the math stays in Python |
| File I/O on inputs (receipts folder, bank CSV) | — | Reads the mounted folders, dedupes by sha256 |
| Mailbox reads (Mailpit API) | — | Yes |
| Extraction, reconciliation math, research, drafting | — | Yes, through `opskit.core` for any model call |
| Approvals, audit, run records | — | Yes, through `opskit.core` |

**Rule: no Code nodes, but real n8n orchestration.** The canvas uses n8n's own nodes for everything that is orchestration:
- IF and Switch on labels the helper returns, for example the triage label;
- the Wait node for approvals;
- the Send Email node for approved replies;
- Convert to File plus the disk-write node for the spreadsheet.

Anything that is logic (extraction, reconciliation math, research, drafting) is an HTTP Request to the helper, where it is typed and tested. A node that would need an expression longer than a field lookup is a sign the logic belongs in Python.

**Why the helper reads the input folder instead of n8n's Local File Trigger:** n8n 2.0 disables `LocalFileTrigger` by default and restricts file access to `~/.n8n-files`. File-watch events are also unreliable on Docker bind mounts. So receipts and inbox each use a Schedule trigger, which calls `GET /v1/<workflow>/pending`. The helper returns new items, keyed by sha256 or Message-ID, and n8n fans out over them. The sample folders are mounted read-only into the helper only. n8n gets one writable `exports/` folder, set through `N8N_RESTRICT_FILE_ACCESS_TO`, for the Phase 2 spreadsheet.

## A2. Containers and boot order

```
kit-secrets ──► postgres ──► migrate ──► seed ◄── mailpit
     │              │                    
     │              └──► n8n-import ──► n8n
     └──────────────────────────────────► api (after migrate)
```

| Service | Image | Runs | Port (host) |
| --- | --- | --- | --- |
| `kit-secrets` | api image | one-shot: create missing secrets in the `kit-secrets` volume and never overwrite | — |
| `postgres` | `pgvector/pgvector:pg17` (digest-pinned) | initdb script creates roles and both DBs from secret files | 127.0.0.1:4302 |
| `migrate` | api image | one-shot: `alembic upgrade heads` as `opskit_owner` | — |
| `mailpit` | `axllent/mailpit` (digest-pinned) | SQLite store on a volume, no relay configured, so nothing leaves the box | 127.0.0.1:4303 web, 127.0.0.1:4304 SMTP |
| `seed` | api image | one-shot, idempotent: sample manifest, demo CRM accounts, 25 emails into Mailpit | — |
| `n8n-import` | n8n image | one-shot, before n8n starts: render credentials from secrets, `import:credentials`, `import:workflow --separate`, `publish:workflow` | — |
| `n8n` | `n8nio/n8n:<2.x pinned>` (digest-pinned) | owner pre-provisioned from env | 127.0.0.1:4300 |
| `api` | built from `docker/api.Dockerfile` | FastAPI and the outbox dispatcher | 127.0.0.1:4301 |

Each secret lives in its own `kit-secrets` volume subpath per consumer (`postgres/`, `n8n/`, `api/`). Each service mounts only its own subpath, read-only, with files at mode 0400 owned by that service's uid. Compose volume `subpath` needs Docker ≥ 26 and Compose ≥ 2.30; the node has 29.1 and 2.40.

Hardening for every service except Postgres: `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]` and a memory limit; `read_only: true` with a `/tmp` tmpfs and a non-root user where the image allows it (Mailpit runs as a non-root user since Phase 1b; a one-shot init job hands it its data volume). Memory limits (n8n 1 GB, Postgres 512 MB, api 512 MB, Mailpit 128 MB). Postgres gets `no-new-privileges` and a memory limit. Dropping its capabilities is tested in the spike and kept only if initdb still works.

## A3. Secrets (no `.env` required to boot)

`kit-secrets` creates these with `secrets.token_hex(32)` (`token_urlsafe(18)` for human passwords) the first time and skips any file that already exists. They live on the named volume `kit-secrets`, in one subpath per consumer, as files at mode 0400 owned by that consumer's uid. **No secret is ever written to a log.** `make login` is the only way to see the two human passwords.

| Secret | Consumers |
| --- | --- |
| `postgres_superuser_password` | postgres |
| `n8n_db_password` | postgres, n8n, n8n-import |
| `opskit_owner_password` (migrations) | postgres, migrate |
| `opskit_app_password` (runtime) | postgres, api, seed |
| `n8n_encryption_key` | n8n, n8n-import |
| `n8n_owner_password` + `.bcrypt` | n8n gets the hash only; the plaintext is shown by `make login` |
| `approver_password` + `.bcrypt` | api gets the hash only; the plaintext is shown by `make login` |
| `approver_session_secret` | api (signs the approver session cookie) |
| `api_service_token` (n8n → api) | api, n8n-import (rendered into an n8n Header Auth credential) |
| `n8n_webhook_token` (callers → n8n webhooks) | n8n-import (Header Auth credential on the smoke webhook), smoke script |

n8n reads `N8N_ENCRYPTION_KEY_FILE` and `DB_POSTGRESDB_PASSWORD_FILE` natively. The owner variables have no `_FILE` form, so a short wrapper entrypoint exports `N8N_INSTANCE_OWNER_PASSWORD_HASH` from its file and then `exec`s the stock entrypoint.

**`.env` is optional** (`env_file: {path: .env, required: false}`). It carries:
- `COMPOSE_PROJECT_NAME` (default `ops-automation-kit`);
- the five host ports `KIT_N8N_PORT=4300`, `KIT_API_PORT=4301`, `KIT_PG_PORT=4302`, `KIT_MAILPIT_WEB_PORT=4303`, `KIT_MAILPIT_SMTP_PORT=4304`, read in `compose.yaml` as `${KIT_N8N_PORT:-4300}` and so on;
- `AGENT_CORE_MODE` (`replay`, `live` or `record`) and `AGENT_CORE_ANTHROPIC_API_KEY`.

A second worktree sets `COMPOSE_PROJECT_NAME=ops-automation-kit-2` and ports 4310–4314. Its volumes, network and containers are then fully separate. `N8N_PUBLIC_URL` and `WEBHOOK_URL` follow `KIT_N8N_PORT`.

## A4. Postgres schema

There are two databases. `n8n` belongs to n8n, which runs its own migrations there. `opskit` is the app's. `REVOKE CONNECT ... FROM PUBLIC` on both, so `n8n_user` cannot reach `opskit` and the app roles cannot reach `n8n`.

The `opskit` database has two roles:
- `opskit_owner` owns the schemas and runs migrations.
- `opskit_app` is the runtime role. It gets `SELECT, INSERT, UPDATE` on working tables, and only `SELECT, INSERT` on `core.audit_log`.

Alembic is split into one branch per domain (`core`, `crm`, `receipts`, `leads`, `inbox`) and run with `alembic upgrade heads`. Phases 2 and 3 run in parallel, and each only adds revisions to its own branch, so their migration heads never conflict.

**Created in Phase 1:**

```sql
-- core: shared by every workflow; only opskit.core writes here
CREATE TABLE core.runs (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow          text NOT NULL CHECK (workflow IN ('kit_smoke', 'receipts', 'leads', 'inbox')),
    n8n_workflow_id   text,
    n8n_execution_id  text UNIQUE,
    mode              text NOT NULL CHECK (mode IN ('replay', 'live', 'record')),
    status            text NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running', 'waiting', 'succeeded', 'failed')),
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz
);

CREATE TABLE core.approvals (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          uuid NOT NULL REFERENCES core.runs (id),
    kind            text NOT NULL,                 -- e.g. 'inbox.reply', 'kit_smoke.echo'
    subject         jsonb NOT NULL,                -- what the human is approving
    edited_subject  jsonb,                         -- set when the approver edits before approving
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    resume_url      text NOT NULL,                 -- signed n8n URL; never returned by the API, never audited
    requested_at    timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    decided_at      timestamptz,
    decided_by      text,
    decision_note   text,
    CHECK ((status = 'pending') = (decided_at IS NULL))
);
CREATE INDEX approvals_pending_idx ON core.approvals (expires_at) WHERE status = 'pending';

CREATE TABLE core.outbox (                         -- resume deliveries to n8n, at-least-once
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    approval_id      uuid NOT NULL UNIQUE REFERENCES core.approvals (id),
    payload          jsonb NOT NULL,
    attempts         integer NOT NULL DEFAULT 0,
    next_attempt_at  timestamptz NOT NULL DEFAULT now(),
    delivered_at     timestamptz,
    last_error       text
);
CREATE INDEX outbox_due_idx ON core.outbox (next_attempt_at) WHERE delivered_at IS NULL;

CREATE TABLE core.audit_log (                      -- append-only: trigger + grants
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at            timestamptz NOT NULL DEFAULT now(),
    run_id        uuid REFERENCES core.runs (id),
    actor         text NOT NULL,                   -- 'n8n', 'approver', 'seed', 'system'
    action        text NOT NULL,                   -- 'approval.requested', 'approval.decided', 'model.call', ...
    subject_type  text,
    subject_id    text,
    details       jsonb NOT NULL DEFAULT '{}'
);
-- BEFORE UPDATE OR DELETE trigger and BEFORE TRUNCATE statement trigger both RAISE EXCEPTION.

CREATE TABLE core.model_calls (                    -- feeds the cost and latency columns of the scorecards
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          uuid REFERENCES core.runs (id),
    prompt_id       text NOT NULL,
    prompt_version  integer NOT NULL,
    tier            text NOT NULL CHECK (tier IN ('small', 'mid', 'large')),
    fixture_key     char(64) NOT NULL,
    mode            text NOT NULL CHECK (mode IN ('replay', 'live', 'record')),
    input_tokens    integer NOT NULL,
    output_tokens   integer NOT NULL,
    cost_usd        numeric(10, 6) NOT NULL,
    latency_ms      integer NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE core.sample_files (                   -- seed manifest: proves what was loaded
    path       text PRIMARY KEY,
    workflow   text NOT NULL,
    kind       text NOT NULL,                      -- 'receipt_image', 'bank_csv', 'company_list', 'corpus_doc', 'email'
    sha256     char(64) NOT NULL,
    bytes      integer NOT NULL,
    loaded_at  timestamptz NOT NULL DEFAULT now()
);

-- crm: the demo CRM is a local table (roadmap decision); seeded with a few existing accounts
CREATE TABLE crm.accounts (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name           text NOT NULL,
    domain         text UNIQUE,
    industry       text,
    employee_band  text,
    hq_city        text,
    description    text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE crm.account_sources (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id  uuid NOT NULL REFERENCES crm.accounts (id),
    field       text NOT NULL,
    source_ref  text NOT NULL,                     -- corpus path or URL
    excerpt     text NOT NULL,
    found_at    timestamptz NOT NULL DEFAULT now()
);
-- Empty schemas with base revisions only: receipts, leads, inbox.
```

Money is stored as integer cents. Dates the business sees use `date`; event times use `timestamptz` in UTC.

**Designed now, created by the phase that owns them** (in `docs/architecture.md`, so Phases 2 and 3 start from the same picture):

| Schema | Tables (key columns) | Phase |
| --- | --- | --- |
| `receipts` | `documents` (sha256 unique, path, media_type), `extractions` (document_id, vendor, receipt_date, subtotal/tax/total_cents, card_last4, confidence jsonb, model_call_id), `bank_transactions` (statement file, row_no, posted_date, description, amount_cents), `matches` (document_id?, bank_txn_id?, status, delta_cents, delta_days, rule), `exports` (path, created_at) | 2 |
| `leads` | `batches` (source file, status), `batch_items` (batch_id, company_name, city_hint), `findings` (item_id, field, value, source_ref, excerpt, conflict bool) | 3 |
| `inbox` | `messages` (Message-ID unique, mailpit_id, from, subject, received_at), `triage` (message_id, category, priority, needs_reply, rationale), `drafts` (message_id, body, approval_id, sent_at) | 3 |

## A5. Workflows as code: export, import, first boot

- The source of truth is `n8n/workflows/*.json`. Every workflow and credential has a fixed ID. Credentials are referenced by ID, `pinData` is stripped, and no secret is ever inline.
- `scripts/lint_workflows.py` (run in CI) rejects:
  - Code and Execute Command nodes;
  - inline credential data;
  - `pinData`;
  - hardcoded `localhost` or IP URLs;
  - a missing fixed `id`.

  Helper URLs must start with `http://api:8000/`, the internal Compose service name. n8n keeps env access blocked in nodes.
- **First boot with no clicks:**
  1. `n8n-import` runs *before* the n8n server starts (`depends_on: condition: service_completed_successfully`). n8n's CLI works directly on the database. Changes made while the server is running only take effect after a restart, so the import is done first.
  2. It renders `credentials.json` from the secret files into tmpfs: the Header Auth for api calls, the Header Auth for the smoke webhook, and the SMTP credential for Mailpit. It runs `n8n import:credentials` and deletes the file. Credentials are generated, not edited, so they are re-imported on every boot.
  3. **Workflows are hash-gated.** For each `n8n/workflows/<file>.json` it compares the file's sha256 with the one recorded at the last import, in `/home/node/.n8n/kit-import-state/<id>.sha256` on the `n8n-data` volume.

     | State | Action |
     | --- | --- |
     | No hash recorded (first boot) | Import and publish; log `INFO imported <name>` |
     | Hash unchanged | Skip, so editor work is kept |
     | Hash changed | Import and publish, then log `WARNING <name>: committed JSON changed since last import; editor changes to this workflow were replaced` |
     | `FORCE_REIMPORT=1` (`make reimport`) | Import and publish everything, with the same warning per workflow |

     Imports land deactivated, so each import is followed by `n8n publish:workflow --id=…` for workflows marked published. In 2.x, publishing replaces the old `--active` toggle.
  4. `n8n` starts. `N8N_INSTANCE_OWNER_MANAGED_BY_ENV=true` with `N8N_INSTANCE_OWNER_EMAIL=owner@kit.example`, first and last name, and `N8N_INSTANCE_OWNER_PASSWORD_HASH` (bcrypt) sets up the owner and skips the signup screen (n8n ≥ 2.17).
  5. Other n8n env settings: `DB_TYPE=postgresdb`, `N8N_DIAGNOSTICS_ENABLED=false`, `N8N_VERSION_NOTIFICATIONS_ENABLED=false`, `N8N_PERSONALIZATION_ENABLED=false`, `N8N_PUBLIC_API_DISABLED=true`, `N8N_SECURE_COOKIE=false` (plain HTTP on localhost only, documented), `WEBHOOK_URL=http://localhost:${KIT_N8N_PORT}/`, `GENERIC_TIMEZONE=UTC`, `N8N_BLOCK_ENV_ACCESS_IN_NODE=true` (the 2.x default, kept), and `EXECUTIONS_DATA_PRUNE=true`.
- **`make export`** runs `n8n export:workflow --all` inside the container, then:
  - normalizes each export (strips `pinData`, `versionId`, `updatedAt`, `meta.instanceId`, sorts keys);
  - writes it over the matching `n8n/workflows/<file>.json`, matched by ID;
  - records the new hash, so the next boot does not re-import or warn.

  `make reimport` sets `FORCE_REIMPORT=1`, re-runs `n8n-import` and restarts n8n.
- **Phase 1 workflows:**

  | ID | Name | Published | Shape |
  | --- | --- | --- | --- |
  | `kitSmoke00000001` | Kit smoke: approval round-trip | yes | Webhook (header auth) → `POST /v1/runs` → `POST /v1/smoke/classify` (replayed model call) → `POST /v1/approvals` (with `$execution.resumeUrl`) → Wait (on webhook call, 10 min limit) → IF `decision == approved` → Send Email (SMTP → Mailpit) → `POST /v1/runs/{id}/finish` |
  | `receipts00000001` | Receipts → reconciled sheet (skeleton) | no | Schedule (15 min) → `GET /v1/receipts/pending` → Split Out → NoOp "Phase 2: extract, reconcile, Convert to File" |
  | `leads00000000001` | Companies → enriched CRM (skeleton) | no | Manual → `GET /v1/leads/pending` → Split Out → NoOp "Phase 3" |
  | `inbox00000000001` | Inbox triage with approvals (skeleton) | no | Schedule (5 min) → `GET /v1/inbox/pending` → Split Out → NoOp "Phase 3: triage, Switch on label, Wait, Send Email" |

  The three `pending` endpoints only list inputs: files found, companies in the CSV, Mailpit messages not yet triaged. They show that the mounts and Mailpit access work. No workflow logic.

## A6. How an n8n execution waits on a human approval and resumes

```
n8n execution                         helper API                              approver (browser)
─────────────                         ──────────                              ──────────────────
HTTP POST /v1/approvals  ───────────► validate resume_url; INSERT approval
  {run_id, kind, subject,              (pending); audit approval.requested
   resume_url: $execution.resumeUrl,  ◄─── 201 {approval_id}
   expires_in}
Wait (On Webhook Call, POST,
  limit = expires_in)
  → execution offloaded to n8n's                                         GET  /approver/login
    Postgres, status "waiting";                                          POST /approver/login (password, CSRF)
    survives restarts                                                    GET  /approver/ (pending list)
                                                                         POST /approver/approvals/{id}/decision
                                      one transaction:                   ◄── form: decision, note, csrf_token
                                        UPDATE … WHERE status='pending'      session cookie
                                          AND expires_at > now()
                                        (0 rows → 409)
                                        INSERT audit approval.decided
                                        INSERT outbox row
                                      dispatcher, three short steps:
                                        1. claim: FOR UPDATE SKIP LOCKED,
                                           lease 120 s, commit
                                        2. POST internal(resume_url), no
                                           transaction open, 10 s deadline
◄──────────────────────────────────────  body {approval_id, decision,
Wait outputs the body                      edited_subject}
GET /v1/approvals/{id} → status       3. record, own transaction:
IF status == approved                    2xx → delivered_at; audit approval.resumed
  → Send Email node (SMTP)               else → backoff 2^n s, cap 5 min,
                                         audit resume.failed after 10 tries
```

1. **Signed resume URL.** n8n 2.x appends `?signature=<HMAC>` to `$execution.resumeUrl`, so execution IDs cannot be guessed. The helper stores the whole URL and never builds one itself.
2. **SSRF guard.** `opskit.approvals.resume.internal_resume_target(url)` accepts only an origin in `{N8N_PUBLIC_URL, N8N_INTERNAL_URL}` with a path matching `^/webhook-waiting/[0-9]+$` and a `signature` query. It always dispatches to `N8N_INTERNAL_URL` (`http://n8n:5678`) plus that path and query. Anything else gets a 422 at creation time.
3. **Secret handling.** The resume URL is a bearer capability. It is excluded from API responses, the approver page, audit details and logs, and the `httpx` logger is held at WARNING so request URLs are never logged.
4. **The approver page is a separate door.** It lives in the helper under `/approver/`. It is server-rendered with Jinja: no JavaScript build and no client-side state.
   - **Login:** one approver account. The password is generated (A3), checked against its bcrypt hash, and shown only by `make login`. After 5 failures in 5 minutes, logins lock out for 5 minutes.
   - **Sessions are server-side** (since Phase 1b). Logging in creates a row in `core.approver_sessions` with its own CSRF token and an absolute 8-hour expiry; the cookie carries only the signed session id. Logout revokes the row, so a copied cookie stops working at once. Before login, a signed cookie carries only the login form's CSRF token.
   - **Session cookie:** signed with `approver_session_secret`. `HttpOnly`, `SameSite=Strict`, `Path=/approver`. `Secure` is off because the page is plain HTTP on 127.0.0.1; the README says so.
   - **CSRF:** a per-session synchronizer token in a hidden field, compared with `hmac.compare_digest` on every POST: login, decision and logout. A missing or wrong token gets a 403.
   - **Audit:** every decision writes `approval.decided` with `actor='approver'`. Every login success and failure writes `approver.login` with the outcome, never the password.
   - **n8n's token cannot reach it.** `/approver/*` accepts only the session cookie. A Bearer service token there gets a 401. The service routes under `/v1/*` never accept the session cookie, and no `/v1` route can decide an approval.
5. **Defense in depth.** After the Wait node resumes, the workflow reads the decision from `GET /v1/approvals/{id}` and branches on that, never on the resume body, since holding the resume URL is not approval. From Phase 3, the endpoint that hands over a draft for sending also requires `status='approved'`.
6. **Timeout.** When the Wait limit elapses, n8n resumes with no body. The workflow then calls `POST /v1/approvals/{id}/expire`. A helper sweeper also expires overdue rows every 60 s. A decision after expiry gets a 409, shown on the page as "expired".
7. **Race.** If a decision lands before n8n has parked the execution, the resume call fails and the outbox retries it. Delivery is at-least-once; the IF branch keys on `approval_id`, and side effects are idempotent per approval.

## A7. Replay mode and cassettes

Model calls go through agent-core v0.1.0a2. Its settings are in `config/agent-core.toml`; `AGENT_CORE_MODE` overrides `mode`.

- **Modes:**

  | Mode | Behaviour |
  | --- | --- |
  | `replay` (default) | Serves recordings. No key, never spends. |
  | `live` | Calls the model. Needs `AGENT_CORE_ANTHROPIC_API_KEY`. |
  | `record` | Calls the model and writes recordings. Needs the same key. |

- **Layout** (agent-core format 2, keyed by content):

  ```
  <cassette_dir>/prompts/<prompt_id>/v<version>/<key>.json
  ```

  The config sets `cassette_dir = "../fixtures/cassettes"` (relative to the config file) and `on_secret = "refuse"`. The repo holds `prompts/receipts.extract/v1/` and `prompts/smoke.classify/v1/`.
- **Misses:** a replay miss raises. It never falls back to a live call.
- **Versioning:** editing a prompt template or output schema without bumping the prompt's version raises `StaleRecordingError`.
- **PDF budgeting:** receipts come from users, so `routing.count_pdf_pages = false`: every PDF is budgeted at the API's 100-page ceiling. That worst case costs $0.88 on the small tier, so `routing.budget_usd_per_call` is `"1.00"` and `on_budget_exceeded = "raise"`. Actual spend is billed on real tokens, about $0.002 per receipt.
- **The kit's own caps** (`src/opskit/receipts/extraction.py`), enforced before any call:
  - images: 4 MiB and 2048 px on the long edge (larger images are downscaled, EXIF orientation applied);
  - PDFs: 5 MiB and at most 2 pages;
  - a file that is unreadable or still over a cap is flagged `needs_review` with a reason and never sent.
- Recordings are made from the host with `AGENT_CORE_MODE=record uv run python -m opskit.evals.receipts`, never by hand. Hand-written recordings never score extraction.

## A8. Licensing

- This repo's own code is MIT (`LICENSE`).
- The README carries one neutral line: n8n is under its Sustainable Use License, and this repo pulls the official n8n image rather than redistributing n8n.
- The bundled receipt fonts are under the SIL Open Font License, with `OFL.txt` alongside them.

## A9. The adapter (`opskit.core`): the only door to models, approvals and audit

`opskit.core.ports` re-exports agent-core's types, so the rest of the app imports only `opskit.core`. import-linter forbids `anthropic` and `aox_agent_core` outside it, and only `opskit.core` may reference the `core.approvals`, `core.audit_log` and `core.model_calls` tables.

| Concern | In the kit |
| --- | --- |
| Mode | agent-core's `Mode`: `replay` (default), `live`, `record`, set by `AGENT_CORE_MODE`. |
| Prompts, tiers, attachments | agent-core's `Tier`, `PromptRef` and `Attachment` (`Attachment.from_bytes`). |
| Run context | agent-core's `RunContext(run_id=str(uuid), external_ids={"workflow", "n8n_workflow_id", "n8n_execution_id"})`. |
| Models | agent-core's `ModelClient.call(...) -> CallResult`, wrapped by `MeteredModelClient`, which writes `core.model_calls` and a `model.call` audit record after each call. |
| Approvals | `PgApprovalQueue` implements agent-core's `ApprovalQueue`: `submit` (with an extra `resume_url` keyword), `get`, `list_pending(principal, after=UUID)`, `resolve(principal)` and `consume`. `RoleApproverPolicy` runs inside `resolve`. The kit keeps its own extras: `expire`, `expire_due`, `list_pending_page` (string cursor, `Page`) and the outbox. |
| Audit | `PgAuditLog` implements agent-core's `AuditLog`: `append(AuditEvent) -> AuditRecord`, `iter_records`, `head`, `verify`. Records are hash-chained with agent-core's `compute_record_hash` (schema 2), and appends are serialised with a transaction-scoped advisory lock. `append_in(session, event)` writes inside a caller's transaction. |
| Principals | The approver is `Principal("approver", HUMAN, {"approver"})` and `required_role = "approver"`. n8n is `Principal("service.n8n", SERVICE)`, which can request approvals but never resolve them. |

The Phase 1 audit table is kept read-only as `core.audit_log_v1`; migration `core_0003` creates the hash-chained `core.audit_log` with the same append-only triggers and grants.

## A10. Receipts

### Reconciliation rules

`src/opskit/receipts/reconcile.py` is pure: integer cents, dates from the inputs, no model calls, no clock. Debits are compared by absolute value. Output does not depend on receipt input order.

1. **Out of scope.** A bank line whose description contains `ACH CREDIT`, `TRANSFER` or `DEPOSIT` is `out_of_scope` and never matched. Any other credit is in scope only against a refund receipt (negative total); a credit with no match is also `out_of_scope`.
2. **Candidates.** A receipt and a bank line are candidates when the amounts are equal, or within 20 % of the receipt amount with a merchant-token match. The posting date runs from 1 day before to 10 days after the receipt date. Merchant similarity is the share of the shorter side's tokens that match, after removing processor prefixes (`SQ *`, `TST*`, `PAYPAL *`, `PP *`), non-letters and corporate words (LLC, INC and similar). A token also matches by a prefix of 4 or more characters, so a truncated descriptor still counts.
3. **Assignment.** Greedy, best first: exact amount, then merchant similarity, then smallest absolute lag. Each receipt and each bank line is used once.
4. **Duplicate receipt.** The first receipt (by file name) with a given vendor, date and total is the original. Later copies are `duplicate_receipt` with no bank line. This is decided before assignment, so two copies never compete for one charge.
5. **Labels for a matched pair.** Amounts differ: `amount_mismatch` (wins over date). Lag of 0 to 3 days: `matched`. Longer lag: `date_drift`.
6. **Leftovers.** A receipt with no candidate is `missing_in_bank`. An unused bank debit is `duplicate_charge` when an earlier matched line has the same descriptor and amount, 0 to 7 days before it; otherwise `unreceipted_charge`.
7. **Needs review.** A receipt flagged `needs_review` in extraction is reported as such and never matched.

Every paired row carries `delta_cents` (bank amount minus receipt amount, both absolute) and `delta_days` (posting date minus receipt date). Rows with no pair leave both empty. The summary counts each status and a `flagged` total: every status except `matched` and `out_of_scope`.

### Workflow node chain

`n8n/workflows/01-receipts.json`, with no Code nodes. A schedule trigger (every 15 minutes) and a header-authenticated webhook (`receipts-run`) both start it.

```
Every 15 minutes / Webhook
  -> Start run (POST /v1/runs)
  -> List new receipts (GET /v1/receipts/pending)
  -> Anything new?  -- no -> Finish run (nothing new)
  -> One item per receipt (Split Out)
  -> Extract receipt (POST /v1/receipts/extract, one per receipt)
  -> Collect results (Aggregate)
  -> Reconcile (POST /v1/receipts/reconcile)
  -> One item per row (Split Out)
  -> Spreadsheet (Convert to File, XLSX, sheet "Reconciliation")
  -> Write to exports (/home/node/exports)
  -> Any flags?  -- yes -> Send summary email (SMTP to Mailpit) -> Finish run
                 -- no  -> Finish run
```

The stack mounts `./exports` into n8n, and n8n may read and write files only there, so `exports/` must exist and be writable by uid 1000 before the stack starts.

## Health endpoints

- `GET /healthz` is unauthenticated and cheap. It returns `status`, `version`, `commit` and `branch` (baked in at build time; null for a dirty tree, a detached HEAD or a non-git build — never guessed), `commit_source` (`"process_start"`), `schema_version` (the applied Alembic heads, sorted and comma-joined; null when the database cannot be read within 1 s, with the reason logged) and `uptime_s`. It answers 200 even when the database is down.
- `GET /readyz` answers 200 only when the database answers.
