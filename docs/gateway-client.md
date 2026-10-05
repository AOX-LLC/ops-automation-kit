# Gateway tool client

An opt-in client in the helper API that calls a company's tools through an AI gateway over MCP
(streamable HTTP, protocol 2025-11-25). It is built against ai-gateway v0.1.0, for the integrated
demo where the helper API looks up a customer and asks for a support ticket. **It is off by
default. With it off, nothing in this kit's demo, workflows, evals or smoke test changes.**

## What it does

Four tools, as the gateway offers them to the `harborline-helper-api` client:

| Method | Gateway tool | Effect |
| --- | --- | --- |
| `search_accounts(ctx, query, limit=…)` | `crm__search_accounts` | read |
| `get_account(ctx, account_id)` | `crm__get_account` | read |
| `list_deals(ctx, account_id, limit=…)` | `crm__list_deals` | read |
| `create_ticket(ctx, account_id=…, subject=…, description=…)` | `tickets__create_ticket` | write, held for approval |

It enforces what the gateway would refuse anyway, before the call: `limit` from 1 to 5 on every
search and deals call (a call that leaves it out is refused by the gateway), and a ticket
description of at most 1,000 characters. Open the ticket with the account id and a summary in your
own words, never pasted contact details: the gateway's egress layer refuses a write that carries
five or more customer values it read in the last 30 minutes.

## Results, not exceptions

Every call returns one of these. Nothing the gateway does raises.

| Result | When |
| --- | --- |
| `Ok(text, data)` | The call went through. |
| `Pending(approval_id)` | A write is waiting for a person at the gateway. Nothing was forwarded. This is a normal state: the client **does not retry**; call again later with the same arguments to ask the gateway again. |
| `PolicyRefused(request_id, message)` | A gateway layer blocked the call (JSON-RPC -32010). The gateway gives no reason, only a request id. |
| `ApprovalRejected` / `ApprovalExpired` | An approver rejected the call, or the approval lapsed. |
| `NotAvailable` | Unknown tool, or outside this client's scope (-32602, one answer for both). |
| `RateLimited(retry_after_s)` | Too many requests (-32010 with a wait), or HTTP 429 after repeated failed logins. |
| `Unauthorized` | HTTP 401: the token is wrong, revoked or expired. |
| `UpstreamError(text)` | The tool server failed behind the gateway. |
| `Unavailable(reason)` | No usable answer: unreachable, timed out, or an unexpected reply. `reason` is a fixed phrase, never exception text. |

A held write waits up to 45 seconds at the gateway before it answers pending, so the client's
timeout is 60 seconds and cannot be set below 50. Each call opens its own session, so the timeout
bounds each round trip, and the whole call also has a deadline of that plus 15 seconds. A result
over 1,000,000 characters is not read (`Unavailable`). HTTP 401 and 429 are read from the HTTP status:
the MCP SDK folds both into a generic `-32603 Server returned an error response`.

## Turning it on

| Setting | Default | Meaning |
| --- | --- | --- |
| `OPSKIT_GATEWAY_ENABLED` | `false` | Build the client at all. |
| `OPSKIT_GATEWAY_MODE` | `replay` | `replay` serves recordings and needs no gateway or token. `record` and `live` call the gateway. |
| `OPSKIT_GATEWAY_URL` | `http://127.0.0.1:4401/mcp` | The gateway's MCP endpoint. Must be `https` unless the host is this machine, because the token travels in a header; proxy settings in the environment are ignored. |
| `OPSKIT_GATEWAY_TIMEOUT_S` | `60` | 50 to 300. |
| `OPSKIT_GATEWAY_RECORDINGS_DIR` | `/app/fixtures/gateway` | Where recordings are read and written. |
| `GATEWAY_TOKEN_FILE` or `GATEWAY_TOKEN` | unset | The bearer token, for `record` and `live` only. Prefer the file: an environment variable shows in `docker inspect`. |

```python
from opskit.gateway import build_gateway_client

client = build_gateway_client(settings, core.audit)   # None when disabled
outcome = await client.get_account(run_context, "ACC-00003")
```

The gateway binds `127.0.0.1` with a Host allowlist, so a client inside this kit's containers
cannot reach it as it is. Run it from the host, or see the gateway's notes on reaching it from
another machine.

## The token

- It is read from the environment or a file and held as a secret value. It is sent only as the
  `Authorization` header of one HTTP client. Nothing logs, formats or re-raises it; a failure is
  reduced to a fixed phrase and an HTTP status. A JSON-RPC error's own message and data are kept as
  the gateway or the SDK sent them (that is what a recording holds). The SDK's own debug logging,
  which can include arguments, is held at WARNING.
- It never appears in a recording: saving one is refused if its text matches a token shape.
  gitleaks has rules for the gateway's `aig_` tokens and for a `gw_` shape named in its notes, and a
  test scans `fixtures/gateway/` for both.
- For a live run, issue a dedicated short-lived token, write it to a mode-0600 file outside the
  repository, and revoke it when the run is over.

## Audit

Each call leaves one `gateway.call` record in the kit's audit log, written through `opskit.core`:
the tool, a SHA-256 of the arguments, the outcome, the elapsed time, the run id and, when there is
one, the approval id or the gateway's request id. **No argument or result text is stored.** The
audit log's secret scan knows the gateway's token shapes and refuses a record that holds one. If
the record cannot be written, the call raises `GatewayAuditError` carrying the gateway's outcome:
the call happened (a write may have been approved and made), so the caller must not simply repeat it.

## Recordings

`fixtures/gateway/` holds recordings of a real gateway's answers, so unit tests run with the
gateway down: they replay the recordings through the client. The integration test of the audit
rows uses a stand-in transport, not recordings. Ordering is kept per replay transport, so a client
rebuilt for every request starts again at the first outcome.

- One file per call, named by the tool and a hash of the tool, the arguments and a scenario name.
  A file holds the outcomes **in the order the gateway gave them**: a write that came back pending
  and then rejected replays as pending, then rejected. Asking more often than it was recorded is an
  error. The scenario separates calls that are identical on the wire but meet a different state
  (a call made with a bad token, a search made without a limit).
- Replay is strict: a call with no recording is an error, never a guess, and never a live call.
- Recordings come only from `scripts/gateway_live_run.py` against a real gateway, never by hand.
  The script needs the gateway and this kit's stack up and a dedicated token.
- Keep the strings fixed: a recording matches its arguments exactly.

## What 09 needs to know

**`run_id` is not kept by the gateway (confirmed against v0.1.0).** Its documentation says it sends
only its own `_meta` to a tool server, drops every key the client sends, and keys its records on its
own request id. In the live run a call that sent the run id in `_meta` was accepted and answered
normally, and afterwards the run id appeared **0 times** in the gateway's database (a data-only
dump), the gateway's log, and the tool servers' logs. The client therefore sends no run id.
Correlating the two audit logs has to go through something both sides can see:

- This kit's `gateway.call` record carries the run id, the tool, the argument hash, the outcome and
  the time, and the gateway's request id when it gives one.
- **Request ids appear only on policy refusals** (JSON-RPC -32010: a layer's block, or a rejected
  approval), in the error's `data`. A successful result, a pending write (it carries an approval
  id), an unknown tool (-32602) and a 401 carry none, and **no result carried a `_meta`** in any
  recorded call.
- The gateway's `requests` table stores a SHA-256 of the arguments, the client name, the tool, the
  outcome and a timestamp, and no run id. Matching a call on both sides means the client, the tool,
  the time and, if the two hashes are computed the same way, the arguments.

For the integrated demo's second phase, the smallest change that closes the gap is on the
gateway's side: either return the gateway's request id in every result's `_meta`, so this kit can
record it, or accept one client-supplied correlation key and store it in the decision record. Neither
is done here, and nothing in the gateway was changed.

**The classifier judges the arguments of a ticket, and the demo's ticket subject is unclassified.**
It judges each prose string in them: at least 24 characters and 3 words. A text it has no recording
for is recorded as `unclassified` with the code `classifier_unrecorded` (never as clean), and the
call goes on. In the live run the ticket's description was judged from its recording and its subject
was not: the gateway recorded the ticket's `before_call` verdict as `unclassified`, and one of the
two classifier model calls as `unrecorded`. The demo's corpus (`story_09.toml`) has a recording for
the description and none for the subject, so the integrated demo needs the exact subject it will use
added to the gateway's corpus (or a subject shorter than 24 characters, which is not judged at all).
The reads were all judged from recordings.

## Measured once

On a 7.9 GB host with other workloads running, with this kit's stack and a fresh stack of the
gateway at v0.1.0 both up and the other stack on the machine stopped:

| | Container memory |
| --- | --- |
| This kit (api, n8n, Postgres, Mailpit) | about 580 MiB |
| The gateway stack (gateway, dashboard, three tool servers, Postgres, two purge jobs) | about 570 MiB |

The host showed 2.66 GB available with both up. Adding the MCP SDK grows the helper API's installed
environment by about 24 MB (approximate, from comparing the installed packages), nearly all of it
the `cryptography` package that the SDK's JWT dependency pulls in.

The live run also showed the gateway recording the client's protocol as 2025-11-25, which is also
what the SDK reported, and every held write as a block with the code `approval_pending`, then
`approval_rejected` after the approver's decision.

## Checked, and not checked

Checked in unit tests: the typed results for every shape above, the limits, the audit record's
contents, ordered replay, token refusal in recordings, and that only `opskit.gateway.mcp_transport`
imports the MCP SDK (an import-linter contract and a test). Checked in an integration test: the audit
rows in the real database, written as the requester role. Checked once against a real gateway
(v0.1.0): the recordings, and five `gateway.call` audit rows for a run, written as the requester
role, with no argument text in any of them.

Not covered: approving or expiring a write (a person's decision at the gateway; an expired
approval takes 30 minutes to occur, so it is not recorded), a gateway reached over TLS, and live
runs under load.
