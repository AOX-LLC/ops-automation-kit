"""robots.txt for live company research, fetched through the guarded client.

The decision follows RFC 9309 with one stricter rule:

- 2xx: the rules apply for our user-agent token;
- 4xx: the file is "unavailable", so everything is allowed, except 429 (the site is asking
  us to slow down), which blocks the site like a server error;
- 5xx, 429, any other status, a timeout, a refused or failed connection, a redirect off the
  site or past the redirect cap, a non-text or oversized file: the whole site is blocked.

The stdlib `RobotFileParser.read()` is not used: it reads 401 and 403 as "disallow all",
which RFC 9309 says to treat as allowed. Only `parse()` is used, on text we fetched ourselves.
Redirects follow the fetcher's own cap and guards. One decision is cached per host for the
life of the cache, and the cache is made once per run.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from opskit.leads.netguard import USER_AGENT, FetchRefused, FetchResult

AGENT_TOKEN = USER_AGENT.split("/")[0]  # "ops-automation-kit"; the full string goes on the wire


class Fetcher(Protocol):
    async def fetch(self, url: str, *, site: str | None = None) -> FetchResult: ...


@dataclass(frozen=True, slots=True)
class RobotsDecision:
    host: str
    reason: str
    blocked: bool
    parser: RobotFileParser | None = None

    def allows(self, url: str) -> bool:
        if self.blocked:
            return False
        if self.parser is None:
            return True
        return self.parser.can_fetch(AGENT_TOKEN, url)


def decide(host: str, result: FetchResult) -> RobotsDecision:
    status = result.status
    if 200 <= status < 300:
        parser = RobotFileParser()
        parser.parse(result.text.splitlines())
        return RobotsDecision(host, "robots.txt rules apply", blocked=False, parser=parser)
    if status == 429:
        return RobotsDecision(host, "robots.txt returned 429: site blocked", blocked=True)
    if 400 <= status < 500:
        return RobotsDecision(host, f"robots.txt returned {status}: allowed", blocked=False)
    return RobotsDecision(host, f"robots.txt returned {status}: site blocked", blocked=True)


class RobotsCache:
    """One robots.txt fetch per host, shared by every company researched in the run."""

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher
        self._decisions: dict[str, asyncio.Task[RobotsDecision]] = {}

    async def decision(self, host: str) -> RobotsDecision:
        host = host.lower()
        task = self._decisions.get(host)
        if task is None:
            task = asyncio.ensure_future(self._lookup(host))
            self._decisions[host] = task
        return await asyncio.shield(task)

    async def decision_for(self, url: str) -> RobotsDecision:
        host = (urlsplit(url).hostname or "").lower()
        return await self.decision(host)

    async def _lookup(self, host: str) -> RobotsDecision:
        try:
            result = await self._fetcher.fetch(f"https://{host}/robots.txt")
        except FetchRefused as exc:
            return RobotsDecision(host, f"robots.txt unreachable ({exc}): site blocked", True)
        return decide(host, result)
