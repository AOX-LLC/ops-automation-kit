"""One manual live research run against a single public site. Nothing is stored or committed.

    AGENT_CORE_MODE=live uv run python -m opskit.evals.live_leads \\
        --website automatedoperationsexperts.com --name "Automated Operations Experts"

Only for a site you own or whose robots.txt and terms allow automated access. It fetches
robots.txt, then at most five fixed paths through the guarded fetcher, asks the small model
for cited fields, checks every quote, and prints what was fetched, each robots decision,
the fields with their sources, and the cost. It spends the caller's own key
(AGENT_CORE_ANTHROPIC_API_KEY) on one model call.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from aox_agent_core import AgentClient, Mode, RunContext, load_config

from opskit.leads.extraction import research_company
from opskit.leads.netguard import GuardedFetcher
from opskit.leads.retrieval import Company, Retrieval, WebRetriever
from opskit.leads.robots import RobotsCache

REPO = Path(__file__).resolve().parents[3]


class _Fetched:
    def __init__(self, retrieval: Retrieval) -> None:
        self._retrieval = retrieval

    async def fetch(self, company: Company) -> Retrieval:
        return self._retrieval


async def run(company: Company) -> dict[str, object]:
    config = load_config(REPO / "config" / "agent-core.toml")
    if config.mode is not Mode.LIVE:
        raise SystemExit("set AGENT_CORE_MODE=live: this run goes to the real site and model")
    fetcher = GuardedFetcher()
    try:
        robots = RobotsCache(fetcher)
        retrieval = await WebRetriever(fetcher, robots).fetch(company)
        decision = await robots.decision(company.website or "")
    finally:
        await fetcher.aclose()
    report: dict[str, object] = {
        "robots": decision.reason,
        "fetch_log": retrieval.notes,
        "pages_read": [doc.url for doc in retrieval.documents],
        "unresolved": retrieval.unresolved_reason,
    }
    if not retrieval.documents:
        return report
    async with AgentClient(config) as client:
        ctx = RunContext(run_id="live-demo", external_ids={"workflow": "leads_live_demo"})
        outcome = await research_company(client, ctx, _Fetched(retrieval), company)
    report |= {
        "fields": {k: (v.model_dump() if v else None) for k, v in outcome.fields.items()},
        "findings": [f.model_dump() for f in outcome.findings],
        "citations": {"returned": outcome.raw_cites, "valid": outcome.valid_cites},
        "cost_usd": outcome.cost_usd,
        "latency_ms": outcome.latency_ms,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--website", required=True, help="bare domain, like acme.example")
    parser.add_argument("--name", required=True)
    parser.add_argument("--city", default="", help="optional, only to tell similar names apart")
    args = parser.parse_args()
    report = asyncio.run(run(Company(args.name, args.city, args.website)))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
