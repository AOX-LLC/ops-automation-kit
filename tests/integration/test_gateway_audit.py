"""09·1: the gateway client's calls are audited through the requester role, and the audit row
holds the run id, a hash of the arguments and the outcome, never the arguments.

The transport here is a stand-in that answers in the gateway's shapes: this test is about the
audit rows in the real database, not about the wire (the recordings cover that).
"""

from __future__ import annotations

import json
import textwrap
from uuid import uuid4

import httpx
import pytest

from tests.integration.conftest import compose, psql

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("finish_test_runs")]

MARKER = "MARKER-ARGUMENT-TEXT-DO-NOT-STORE"

SCRIPT = textwrap.dedent(
    """
    import asyncio, json, sys
    from uuid import UUID
    from opskit.config import Settings
    from opskit.core.pg.audit import PgAuditLog
    from opskit.core.ports import RunContext
    from opskit.db.engine import make_engine, make_session_factory
    from opskit.gateway.client import GatewayClient
    from opskit.gateway.transport import RawOutcome

    ANSWERS = [
        RawOutcome(kind="result", text="found"),
        RawOutcome(
            kind="result",
            is_error=True,
            structured={"status": "approval_pending", "approval_id": "appr-1"},
        ),
        RawOutcome(kind="rpc_error", code=-32010, message="Request blocked by gateway policy."),
    ]

    class Stub:
        def __init__(self):
            self.answers = iter(ANSWERS)
        async def call(self, tool, arguments, *, meta=None):
            return next(self.answers)

    async def main():
        engine = make_engine(Settings())
        client = GatewayClient(Stub(), PgAuditLog(make_session_factory(engine)))
        ctx = RunContext(run_id=sys.argv[1])
        outcomes = [
            await client.search_accounts(ctx, "MARKER", limit=5),
            await client.create_ticket(
                ctx, account_id="ACC-00003", subject="Subject text", description="MARKER_DESC"
            ),
            await client.get_account(ctx, "ACC-00003"),
        ]
        print(json.dumps([type(o).__name__ for o in outcomes]))
        await engine.dispose()

    asyncio.run(main())
    """
)


def test_gateway_calls_are_audited_with_the_run_id_and_no_arguments(service: httpx.Client) -> None:
    run = service.post(
        "/v1/runs", json={"workflow": "kit_smoke", "n8n_execution_id": f"itest-{uuid4()}"}
    )
    assert run.status_code == 201, run.text
    run_id = run.json()["run_id"]
    script = SCRIPT.replace("MARKER_DESC", MARKER).replace('"MARKER"', f'"{MARKER}"')

    result = compose("exec", "-T", "api", "python", "-c", script, run_id, check=False)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == ["Ok", "Pending", "PolicyRefused"]
    rows = psql(
        "select payload, run_context, db_login from core.audit_log "
        f"where action = 'gateway.call' and run_context like '%{run_id}%' order by seq"
    )
    assert rows.returncode == 0, rows.stderr
    lines = rows.stdout.strip().splitlines()
    assert len(lines) == 3
    assert MARKER not in rows.stdout
    payloads = [json.loads(line.split("|")[0]) for line in lines]
    assert [p["outcome"] for p in payloads] == ["Ok", "Pending", "PolicyRefused"]
    assert all(len(p["arguments_sha256"]) == 64 for p in payloads)
    assert payloads[1]["approval_id"] == "appr-1"
    assert {line.split("|")[2] for line in lines} == {"opskit_app"}
