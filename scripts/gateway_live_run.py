"""Records the gateway client's calls against a real ai-gateway, from the host.

Run by hand, never in CI, with the gateway and this kit's stack up and a dedicated short-lived
token in the file GATEWAY_TOKEN_FILE (the file is read, never printed). It writes the recordings
under fixtures/gateway/ and one `gateway.call` audit row per call through the requester role.

    GATEWAY_TOKEN_FILE=... OPSKIT_GATEWAY_ENABLED=true OPSKIT_GATEWAY_MODE=record \\
    OPSKIT_SECRETS_DIR=<dir holding opskit_app_password> OPSKIT_DB_HOST=127.0.0.1 \\
    OPSKIT_DB_PORT=4302 AGENT_CORE_CONFIG=config/agent-core.toml \\
    uv run python scripts/gateway_live_run.py --steps read,probe,failures,ticket

The ticket step opens the story's ticket, which the gateway holds for a person. The script then
waits for the file named by --wait-for to appear (touch it after you have rejected the request
with gateway-approver) and asks again, so the rejection is recorded after the pending answer.
It approves nothing.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from pydantic import SecretStr

from opskit.config import Settings
from opskit.core.factory import build_core
from opskit.core.ports import RunContext, run_uuid
from opskit.db.engine import make_engine, make_session_factory
from opskit.gateway.client import GatewayClient, classify
from opskit.gateway.mcp_transport import McpTransport
from opskit.gateway.recordings import RecordingStore, RecordingTransport
from opskit.gateway.transport import PROTOCOL_VERSION, RawOutcome

ACCOUNT_ID = "ACC-00003"
# The exact text of "ticket-damaged-order" in ai-gateway's config/classifier_corpus/story_09.toml,
# so the gateway's classifier answers from its recordings.
TICKET_DESCRIPTION = (
    "Customer Marta (Pier Nine Supply, ACC-00003) reports that two of four pallets on order "
    "HS-48213 arrived damaged: torn shrink wrap and crushed cartons. She asks for replacements "
    "and return instructions."
)
TICKET_SUBJECT = "Damaged pallets on order HS-48213"
# A made-up value, built at run time so no token-shaped literal sits in the repository.
NOT_A_TOKEN = "aig_" + "zzzzzzzz" + "_" + "z" * 43
POLL_SECONDS = 2


def _show(label: str, raw: RawOutcome) -> None:
    summary = raw.to_json()
    if raw.kind == "result" and not raw.is_error:
        summary = {
            "kind": "result",
            "text_chars": len(raw.text),
            "structured_keys": sorted(raw.structured or {}),
        }
    print(f"{label}: {type(classify(raw)).__name__} {summary}")


async def _read_steps(client: GatewayClient, ctx: RunContext) -> None:
    print("search:", type(await client.search_accounts(ctx, "Pier Nine", limit=5)).__name__)
    print("get:", type(await client.get_account(ctx, ACCOUNT_ID)).__name__)
    print("deals:", type(await client.list_deals(ctx, ACCOUNT_ID, limit=5)).__name__)


async def _probe_step(live: McpTransport, store: RecordingStore, ctx: RunContext) -> None:
    """One call that sends a run id in _meta, to see what the gateway does with it."""
    recorder = RecordingTransport(live, store, scenario="meta-probe")
    raw = await recorder.call(
        "crm__get_account", {"account_id": ACCOUNT_ID}, meta={"io.aox.opskit/run_id": ctx.run_id}
    )
    _show("meta probe", raw)


async def _failure_steps(settings: Settings, live: McpTransport, store: RecordingStore) -> None:
    bad = McpTransport(settings.gateway_url, SecretStr(NOT_A_TOKEN), settings.gateway_timeout_s)
    unauthorized = RecordingTransport(bad, store, scenario="unauthorized")
    _show("bad token", await unauthorized.call("crm__get_account", {"account_id": ACCOUNT_ID}))
    # The client would refuse a search without a limit, so this goes around it, to record the
    # gateway's own refusal.
    no_limit = RecordingTransport(live, store, scenario="no-limit")
    _show(
        "search without limit", await no_limit.call("crm__search_accounts", {"query": "Pier Nine"})
    )
    out_of_scope = RecordingTransport(live, store, scenario="out-of-scope")
    _show(
        "tool outside scope",
        await out_of_scope.call("tickets__add_comment", {"ticket_id": "TKT-000001", "body": "x"}),
    )


async def _ticket_step(client: GatewayClient, ctx: RunContext, wait_for: Path) -> None:
    arguments = {
        "account_id": ACCOUNT_ID,
        "subject": TICKET_SUBJECT,
        "description": TICKET_DESCRIPTION,
    }
    first = await client.create_ticket(ctx, **arguments)
    print("ticket:", type(first).__name__, first)
    print(f"waiting for {wait_for} (reject the request with gateway-approver, then touch it)")
    while not await asyncio.to_thread(wait_for.exists):  # noqa: ASYNC110 - a person creates the file
        await asyncio.sleep(POLL_SECONDS)
    second = await client.create_ticket(ctx, **arguments)
    print("ticket after rejection:", type(second).__name__, second)


async def main(steps: set[str], wait_for: Path) -> None:
    settings = Settings()
    engine = make_engine(settings)
    core = build_core(settings, make_session_factory(engine))
    store = RecordingStore(
        settings.gateway_recordings_dir,
        recorded_with={
            "gateway": "ai-gateway v0.1.0",
            "mcp_sdk": "2.2.0",
            "protocol": PROTOCOL_VERSION,
        },
    )
    live = McpTransport(
        settings.gateway_url, settings.read_gateway_token(), settings.gateway_timeout_s
    )
    client = GatewayClient(RecordingTransport(live, store), core.audit)
    ctx = await core.runs.start(workflow="inbox", n8n_workflow_id=None, n8n_execution_id=None)
    print("run id:", ctx.run_id)
    try:
        if "read" in steps:
            await _read_steps(client, ctx)
        if "probe" in steps:
            await _probe_step(live, store, ctx)
        if "failures" in steps:
            await _failure_steps(settings, live, store)
        if "ticket" in steps:
            await _ticket_step(client, ctx, wait_for)
    finally:
        await core.runs.finish(run_uuid(ctx), succeeded=True)
        await engine.dispose()
    print("last transport failure types:", live.last_failure_types)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--steps", default="read,probe,failures,ticket")
    parser.add_argument("--wait-for", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(set(args.steps.split(",")), args.wait_for))
