# ops-automation-kit — working rules

Three standalone n8n + Claude workflows for small businesses, runnable from one
`docker compose up` with fictional sample data:

1. **Receipts:** a folder of receipts becomes a reconciled spreadsheet, with mismatches against a bank CSV flagged.
2. **Leads:** a list of company names becomes enriched CRM records with sources.
3. **Inbox:** inbound emails are triaged, and drafted replies wait for human approval.

## How the pieces split

- **n8n orchestrates.** It owns triggers, schedules, fan-out and branching (IF, Switch), waiting for approvals (Wait), sending approved email (Send Email) and writing output files.
- **The helper API (`src/opskit`, FastAPI) does the work.** It owns file and mailbox reads, extraction, reconciliation math, research, drafting, approvals and audit.
- **No Code nodes.** If a node needs more than a field lookup, the logic belongs in Python.
- **One door to models, approvals and audit:** `opskit.core`. Nothing else imports a model SDK (import-linter enforces this) or writes to `core.approvals`, `core.audit_log` or `core.model_calls` (a rule reviewers check).

## Layout

| Path | What it is |
| --- | --- |
| `compose.yaml` | the stack: secrets init, Postgres, migrate, Mailpit, seed, n8n import, n8n, api |
| `docker/` | api Dockerfile, Postgres init script, n8n entrypoint and import script |
| `n8n/workflows/` | exported workflow JSON, the source of truth for the canvas |
| `src/opskit/` | helper API: `api/`, `core/` (adapter), `approvals/`, `db/`, `seed/`, `bootstrap/` |
| `fixtures/cassettes/` | agent-core recordings (format 2) that replay mode serves; keyed by content |
| `config/agent-core.toml` | agent-core settings: mode, tiers, prices, PDF budgeting, cassette folder |
| `samples/` | fictional runtime inputs, mounted read-only into the api |
| `evals/answer_keys/` | ground truth for scoring; never mounted into a container |
| `tools/samplegen/` | generators for `samples/` and `evals/`; run with `make samples` |
| `scripts/` | smoke test, workflow lint, export, public-safety hook |
| `docs/` | architecture, agent-core needs, phase plans |

## Ports and stack name

| Service | Default host port | `.env` variable |
| --- | --- | --- |
| n8n | 4300 | `KIT_N8N_PORT` |
| helper API and approver page | 4301 | `KIT_API_PORT` |
| Postgres | 4302 | `KIT_PG_PORT` |
| Mailpit web | 4303 | `KIT_MAILPIT_WEB_PORT` |
| Mailpit SMTP | 4304 | `KIT_MAILPIT_SMTP_PORT` |

All ports bind `127.0.0.1`. The Compose project is `ops-automation-kit` (`COMPOSE_PROJECT_NAME`).

**Parallel stacks.** To run a second checkout beside the first, copy `.env.example` to `.env` in that checkout. Set `COMPOSE_PROJECT_NAME=ops-automation-kit-2` and the five ports to 4310–4314. Volumes, network and containers are then separate.

## Worktree flow

- Never edit or commit in the main checkout.
- Each session works in its own worktree on a branch named `phase-N-<slug>`:

  ```sh
  git fetch origin
  git worktree add .worktrees/phase-N-<slug> -b phase-N-<slug> origin/main
  ```
- Copy the gitignored `CLAUDE.local.md` and `.denylist.local` from the main folder into each new worktree; they are lost when a worktree is removed.
- Push with `git push -u origin phase-N-<slug>`, open a PR, and wait for approval. Never merge your own PR.

## Commands

| Command | What it does |
| --- | --- |
| `make up` / `make down` | start or stop the stack (`down` keeps volumes; `make clean` removes them) |
| `make login` | print the n8n owner and approver passwords; the only way to see generated secrets |
| `make check` | ruff, mypy, import-linter, workflow lint, unit tests |
| `make test` | unit tests, then integration tests against the running stack (`make up` first); the integration tests pin the approval state machine, CSRF, token separation, lockout, append-only audit and role isolation |
| `make smoke` | clean compose boot, seeded-data checks, approval round-trip, second boot |
| `make samples` | regenerate `samples/` and `evals/answer_keys/` inside the pinned image |
| `make evals` | score receipts and the inbox in replay; writes `evals/scorecards/` |
| `make export` | write workflows edited in the n8n editor back to `n8n/workflows/` |
| `make reimport` | force re-import of every workflow from the repo (overwrites editor changes) |

On boot, a workflow is re-imported only when its committed JSON changed since the last import. When that replaces editor work, the import log says so with a warning.

## Rules

- One commit per concern. Refactors and behavior changes are separate commits.
- **No attribution trailers or co-author lines in commits or PR descriptions.**
- Public repo: synthetic data only, `.example` domains, 555-01xx phone numbers. No names of real clients, internal products, tools or hosts.
- `.env.example` only; never commit a real key. gitleaks runs in pre-commit and in CI.
- Model calls go through agent-core v0.1.0a6 (pinned in `pyproject.toml`). `AGENT_CORE_MODE=replay` is the default and never spends; `live` and `record` read the viewer's own key from `AGENT_CORE_ANTHROPIC_API_KEY`, never `ANTHROPIC_API_KEY`. Model IDs and prices live in config, never in code.
- Recordings are made from the host (`AGENT_CORE_MODE=record uv run python -m opskit.evals.receipts`, or `.inbox`), never by hand. Hand-written fixtures never score extraction.
- Secrets never go to logs. Generated secrets live on the `kit-secrets` volume; `make login` is the only way to read the human passwords.
- Answer keys under `evals/` are never mounted into a running container.
- Workflow JSON uses fixed IDs and no inline credentials, no `pinData`, and helper URLs under `http://api:8000/`. `scripts/lint_workflows.py` checks this.
