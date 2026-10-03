"""robots.txt decisions: RFC 9309, with 429 treated like a server error."""

from __future__ import annotations

import pytest

from opskit.leads.netguard import FetchRefused, FetchResult
from opskit.leads.robots import AGENT_TOKEN, RobotsCache

HOST = "acme.example"


class FakeFetcher:
    def __init__(self, status: int = 200, text: str = "", error: Exception | None = None) -> None:
        self.status, self.text, self.error = status, text, error
        self.urls: list[str] = []

    async def fetch(self, url: str, *, site: str | None = None) -> FetchResult:
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return FetchResult(url=url, status=self.status, content_type="text/plain", text=self.text)


async def _allowed(fetcher: FakeFetcher, path: str = "/about") -> bool:
    decision = await RobotsCache(fetcher).decision(HOST)
    return decision.allows(f"https://{HOST}{path}")


async def test_a_file_that_allows_the_path() -> None:
    assert await _allowed(FakeFetcher(text="User-agent: *\nDisallow: /admin\n"))


async def test_a_file_that_disallows_the_path() -> None:
    assert not await _allowed(FakeFetcher(text="User-agent: *\nDisallow: /about\n"))


async def test_a_rule_for_our_token_beats_the_wildcard() -> None:
    text = f"User-agent: *\nDisallow:\n\nUser-agent: {AGENT_TOKEN}\nDisallow: /\n"
    assert not await _allowed(FakeFetcher(text=text))


async def test_disallow_everything() -> None:
    assert not await _allowed(FakeFetcher(text="User-agent: *\nDisallow: /\n"), "/")


async def test_an_empty_file_allows_everything() -> None:
    assert await _allowed(FakeFetcher(text=""))


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 451])
async def test_4xx_means_allowed(status: int) -> None:
    assert await _allowed(FakeFetcher(status=status))


async def test_429_blocks_the_site() -> None:
    assert not await _allowed(FakeFetcher(status=429))


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_5xx_blocks_the_site(status: int) -> None:
    assert not await _allowed(FakeFetcher(status=status))


@pytest.mark.parametrize("status", [301, 304, 100])
async def test_a_stray_non_success_status_blocks_the_site(status: int) -> None:
    assert not await _allowed(FakeFetcher(status=status))


@pytest.mark.parametrize(
    "reason",
    [
        "total time limit reached",
        "connection to acme.example failed: ConnectTimeout",
        "connection to acme.example failed: ConnectError",
        "too many redirects",
        "acme.example is not on other.example",
        "page is larger than the size cap",
    ],
)
async def test_a_timeout_refusal_or_guard_failure_blocks_the_site(reason: str) -> None:
    assert not await _allowed(FakeFetcher(error=FetchRefused(reason)))


async def test_the_decision_is_cached_per_host_and_shared_by_concurrent_callers() -> None:
    import asyncio

    fetcher = FakeFetcher(text="User-agent: *\nDisallow:\n")
    cache = RobotsCache(fetcher)
    await asyncio.gather(*(cache.decision(HOST) for _ in range(5)))
    await cache.decision(HOST.upper())
    await cache.allows(f"https://{HOST}/contact")
    assert fetcher.urls == [f"https://{HOST}/robots.txt"]
    await cache.decision("other.example")
    assert len(fetcher.urls) == 2


async def test_a_blocked_decision_is_cached_too() -> None:
    fetcher = FakeFetcher(status=503)
    cache = RobotsCache(fetcher)
    assert not (await cache.decision(HOST)).allows(f"https://{HOST}/")
    assert not (await cache.decision(HOST)).allows(f"https://{HOST}/about")
    assert len(fetcher.urls) == 1
