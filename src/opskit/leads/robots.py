"""robots.txt for live company research, fetched through the guarded client.

The decision follows RFC 9309 with one stricter rule:

- 2xx: the rules apply for our user-agent token;
- 4xx: the file is "unavailable", so everything is allowed, except 429 (the site is asking
  us to slow down), which blocks the site like a server error;
- 5xx, 429, any other status, a timeout, a refused or failed connection, a redirect off the
  site or past the redirect cap, a non-text or oversized file: the whole site is blocked.

The rules are matched by `Rules` below, not by the stdlib `RobotFileParser`: it reads 401 and
403 as "disallow all" (RFC 9309 says allowed), takes the first matching rule instead of the
longest, and has no `*` or `$`. Redirects follow the fetcher's own cap and guards, and the
retriever checks these rules again before each hop. One decision is cached per host for the
life of the cache, and the cache is made once per run.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from opskit.leads.netguard import USER_AGENT, FetchRefused, FetchResult

AGENT_TOKEN = USER_AGENT.split("/")[0]  # "ops-automation-kit"; the full string goes on the wire


class Fetcher(Protocol):
    async def fetch(self, url: str, *, site: str | None = None) -> FetchResult: ...


MAX_RULE_CHARS = 512  # a longer rule or path is not matched; no real site needs one
MAX_RULES = 2_000  # a file with more rules than this blocks the site (fail closed)
MAX_PATH_CHARS = 2_048
MAX_AGENTS_PER_GROUP = 100


def _matches(pattern: str, text: str) -> bool:
    """Prefix match of a robots path pattern: `*` is any run of characters, a trailing `$`
    anchors the end. Linear in practice (it remembers only the last `*`), so a hostile
    robots.txt full of wildcards can't make the matcher backtrack catastrophically."""
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    p = t = 0
    star = mark = -1
    while t < len(text):
        if p < len(body) and body[p] == "*":
            star, mark = p, t
            p += 1
        elif p < len(body) and body[p] == text[t]:
            p += 1
            t += 1
        elif p == len(body) and not anchored:
            return True  # the whole pattern matched a prefix
        elif star != -1:
            mark += 1  # let the last `*` swallow one more character and try again
            t, p = mark, star + 1
        else:
            return False
    while p < len(body) and body[p] == "*":
        p += 1
    return p == len(body)


class Rules:
    """RFC 9309 matching for one crawler: the group for our token (else `*`), the longest
    matching rule wins, and Allow wins a tie. No match means allowed."""

    def __init__(self, text: str) -> None:
        self.too_large = False
        groups: dict[str, list[tuple[bool, str, int]]] = {}
        count = 0
        agents: list[str] = []
        in_rules = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            key, sep, value = line.partition(":")
            if not sep:
                continue
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if in_rules:
                    agents, in_rules = [], False
                if len(agents) >= MAX_AGENTS_PER_GROUP:
                    self.too_large = True
                    continue
                agents.append(value.lower())
                groups.setdefault(agents[-1], [])
            elif key in ("allow", "disallow") and agents:
                in_rules = True
                count += 1
                if count > MAX_RULES or len(value) > MAX_RULE_CHARS:
                    self.too_large = True  # ignoring a rule would fail open, so block instead
                elif value:  # an empty Disallow allows everything: it adds no rule
                    rule = (key == "allow", value, len(value))
                    for agent in agents:
                        groups[agent].append(rule)
        self._rules = groups.get(AGENT_TOKEN.lower(), groups.get("*", []))

    def allows(self, path_and_query: str) -> bool:
        if self.too_large or len(path_and_query) > MAX_PATH_CHARS:
            return False
        best: tuple[int, bool] | None = None
        for allow, pattern, length in self._rules:
            if _matches(pattern, path_and_query) and (
                best is None or length > best[0] or (length == best[0] and allow)
            ):
                best = (length, allow)
        return True if best is None else best[1]


@dataclass(frozen=True, slots=True)
class RobotsDecision:
    host: str
    reason: str
    blocked: bool
    rules: Rules | None = None

    def allows(self, url: str) -> bool:
        if self.blocked:
            return False
        if self.rules is None:
            return True
        parts = urlsplit(url)
        return self.rules.allows((parts.path or "/") + (f"?{parts.query}" if parts.query else ""))


def decide(host: str, result: FetchResult) -> RobotsDecision:
    status = result.status
    if 200 <= status < 300:
        return RobotsDecision(
            host, "robots.txt rules apply", blocked=False, rules=Rules(result.text)
        )
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
            return RobotsDecision(
                host, f"robots.txt unreachable ({exc}): site blocked", blocked=True
            )
        return decide(host, result)
