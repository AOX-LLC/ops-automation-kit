# ops-automation-kit

Three n8n + Claude workflows for small businesses, runnable from one `docker compose up` with fictional sample data.

1. **Receipts:** a folder of receipts becomes a reconciled spreadsheet, with mismatches against a bank CSV flagged.
2. **Leads:** a list of company names becomes enriched CRM records, each field with its source.
3. **Inbox:** inbound email is triaged, and drafted replies are held for human approval before anything is sent.

n8n orchestrates. A small Python helper API does the work.

**Status:** Phase 1: the stack, sample data, mock mode and the approval round-trip are in place; the workflows' logic lands in later phases.

<!-- GIF and eval scorecards arrive with Phase 4 -->

## All data in this repo is fictional

**Every business, person, address, email and number here is invented. Domains use the reserved `.example` TLD. Phone numbers use the 555-01xx fictional range. Receipts are rendered in code.**

## Quickstart

Requirements: Docker Engine 26+ with Compose 2.30+.

```sh
docker compose up -d --wait
```

The first boot takes a few minutes. n8n runs its database migrations before it reports healthy.

| What | URL |
| --- | --- |
| n8n editor | http://localhost:4300 |
| Approver page | http://localhost:4301/approver/ |
| Mailpit (caught email) | http://localhost:4303 |

Passwords are generated on first boot and never logged. `make login` prints the n8n owner password and the approver password. It is the only way to see them.

| Command | What it does |
| --- | --- |
| `make smoke` | End-to-end check: clean boot, seeded data, approval round-trip, second boot |
| `make down` | Stop the stack and keep its volumes |
| `make clean` | Stop the stack and remove its volumes |

## How it fits together

| Concern | n8n | Helper API (`src/opskit`, FastAPI) |
| --- | --- | --- |
| Triggers, schedules, fan-out, branching | Yes | No |
| Waiting for a human | Wait node | Stores the approval, serves the approver page, resumes n8n |
| Sending approved email | Send Email node (SMTP to Mailpit) | Re-checks the approval first |
| Reading files and mailboxes | No | Yes |
| Extraction, reconciliation math, research, drafting | No | Yes |
| Approvals, audit, run records | No | Yes |

**No Code nodes.** If a node needs more than a field lookup, the logic belongs in Python. `scripts/lint_workflows.py` rejects Code and Execute Command nodes.

All model calls, approvals and audit writes go through one module, `opskit.core`. import-linter keeps model SDKs out of everything else.

### The approval round-trip

1. n8n posts the draft to the helper with its own signed resume URL, then stops at a Wait node.
2. The helper stores the approval as pending. The resume URL carries an HMAC signature, so it cannot be guessed.
3. A person opens the approver page and logs in. The login is one password, and every form post carries a CSRF token.
4. The decision is saved in one transaction with an audit row. A dispatcher then calls the resume URL.
5. n8n continues: an IF node checks the decision, and only an approved reply goes to Send Email.

n8n's service token cannot approve anything. The approver page accepts only the session cookie, and no `/v1` route can decide an approval. After resuming, the workflow reads the recorded decision from `GET /v1/approvals/{id}` and branches on it, never on the resume request's body.

**Local-only cookies.** The stack serves plain HTTP on 127.0.0.1, so the approver cookie is sent without `Secure` and n8n runs with `N8N_SECURE_COOKIE=false`. Put TLS in front and turn both back on before exposing either beyond your machine.

## Mock mode and live mode

`MOCK_MODE=true` is the default. No API key is needed. Model calls replay recorded responses from:

```
fixtures/model/<workflow>/<prompt_id>/<key>.json
```

The key is a content hash of the prompt, its inputs and any attachment hashes. A missing fixture is an error, not a silent live call.

Live mode (`MOCK_MODE=false`) needs your own key in `AGENT_CORE_ANTHROPIC_API_KEY` in `.env`. Never use `ANTHROPIC_API_KEY`. Startup fails without the key.

Phase 1 ships only the smoke fixture, `fixtures/model/kit_smoke/`. Live model calls arrive in Phase 2.

## Ports and parallel stacks

All ports bind `127.0.0.1`. Set them in `.env`; copy `.env.example` to start.

| Service | Default | `.env` variable |
| --- | --- | --- |
| n8n | 4300 | `KIT_N8N_PORT` |
| Helper API and approver page | 4301 | `KIT_API_PORT` |
| Postgres | 4302 | `KIT_PG_PORT` |
| Mailpit web | 4303 | `KIT_MAILPIT_WEB_PORT` |
| Mailpit SMTP | 4304 | `KIT_MAILPIT_SMTP_PORT` |

To run a second copy beside the first, use another checkout. In its `.env`, set `COMPOSE_PROJECT_NAME=ops-automation-kit-2` and the five ports to 4310 through 4314. Volumes, network and containers are then separate.

## Sample data

- **Receipts:** 30 receipt images and one bank statement CSV. The mismatch types are amount mismatch, date drift, missing in bank and duplicate receipt.
- **Leads:** 20 company names, a local corpus of about 37 documents standing in for web research, and 5 existing CRM accounts.
- **Inbox:** 25 emails for a fictional plumbing business, plus its business profile.

Answer keys live in `evals/answer_keys/`. They are never mounted into containers. `make samples` regenerates all of it inside the pinned image.

## Editing workflows

Workflows live in `n8n/workflows/` as JSON, and that is the source of truth.

- On boot, a workflow is re-imported only when its committed JSON changed. When that replaces editor work, the `n8n-import` log prints a warning.
- `make export` writes changes made in the n8n editor back to `n8n/workflows/`.
- `make reimport` forces a re-import of every workflow and overwrites editor changes.

Phase 1 workflows: `00-kit-smoke` (the approval round-trip) and skeletons `01-receipts`, `02-leads` and `03-inbox`.

## Repository layout

```
compose.yaml         the stack
docker/              api Dockerfile, Postgres init, n8n entrypoint and import script
n8n/workflows/       workflow JSON, the source of truth
src/opskit/          helper API: api, core, approvals, db, seed, bootstrap
fixtures/model/      recorded model responses for mock mode
samples/             fictional inputs, mounted read-only into the api
evals/answer_keys/   ground truth for scoring, never mounted
tools/samplegen/     generators for samples and answer keys
scripts/             workflow lint, public-safety check
docs/                plans and architecture notes
```

## Licenses

This repository's code is MIT licensed (see LICENSE). n8n is licensed under its Sustainable Use License; this repo pulls the official n8n image and does not redistribute n8n. The receipt fonts are under the SIL Open Font License (tools/samplegen/fonts/OFL.txt).

## Notes

Make.com is a port target mentioned for reference. It is not built.

Troubleshooting:

- A port is already in use: change the matching `KIT_*_PORT` in `.env`.
- Docker is too old: the stack uses volume subpaths, which need Docker Engine 26+.
- arm64 hosts are untested in CI.
