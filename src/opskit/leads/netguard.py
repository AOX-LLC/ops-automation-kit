"""Guarded HTTP fetching for live company research.

Only the company's own public site is fetched, and every request is checked before it is
sent:

- the hostname is resolved once and refused if any address is private, loopback,
  link-local, multicast, reserved, unspecified, CGNAT (100.64.0.0/10) or an IPv4-mapped
  form of those, so a name can't point the fetcher at the network it runs on;
- the connection goes to that vetted IP, so a DNS change between the check and the connect
  (rebinding) can't redirect it, while TLS still verifies the certificate and SNI against
  the original hostname;
- https only, port 443, at most 3 redirects, each re-checked and kept on the same host
  (a leading "www." aside);
- connections are never reused: a pooled connection is keyed by IP, so reusing one for a
  second hostname would skip that hostname's certificate check;
- the response must be uncompressed, and the body cap and the total deadline are enforced
  while streaming, so an oversized or slow page is abandoned mid-download.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

USER_AGENT = "ops-automation-kit/0.1 (+company research)"
MAX_BODY_BYTES = 512 * 1024
CONNECT_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 5.0
TOTAL_TIMEOUT_S = 10.0
MAX_REDIRECTS = 3
ALLOWED_TYPES = ("text/html", "text/plain")
CGNAT = ipaddress.ip_network("100.64.0.0/10")

type Address = ipaddress.IPv4Address | ipaddress.IPv6Address
type Resolver = Callable[[str, int], Awaitable[Sequence[str]]]


class FetchRefused(Exception):
    """The request was not sent, or was abandoned, because a guard refused it."""


def is_public(address: Address) -> bool:
    """True only for globally routable unicast addresses."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if isinstance(address, ipaddress.IPv4Address) and address in CGNAT:
        return False
    blocked = (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )
    if isinstance(address, ipaddress.IPv6Address):
        blocked = blocked or address.is_site_local
    return address.is_global and not blocked


async def system_resolver(host: str, port: int) -> Sequence[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


@dataclass(frozen=True, slots=True)
class FetchPolicy:
    """What the fetcher may reach. Tests narrow `address_ok`; production uses is_public."""

    address_ok: Callable[[Address], bool] = is_public
    max_body_bytes: int = MAX_BODY_BYTES
    total_timeout_s: float = TOTAL_TIMEOUT_S
    ports: frozenset[int] = frozenset({443})


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str
    status: int
    content_type: str
    text: str


def _site(host: str) -> str:
    """The host, less a leading "www.". Redirects stay on it: a sibling subdomain or another
    tenant of a shared suffix (`*.co.uk`, `*.github.io`) is a different site."""
    host = host.lower().rstrip(".")
    return host.removeprefix("www.")


class GuardedFetcher:
    def __init__(
        self,
        *,
        policy: FetchPolicy | None = None,
        resolver: Resolver = system_resolver,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._policy = policy or FetchPolicy()
        self._resolver = resolver
        context = ssl_context or ssl.create_default_context()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        self._client = httpx.AsyncClient(
            verify=context,
            trust_env=False,  # never route through an environment proxy
            follow_redirects=False,
            timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
            limits=httpx.Limits(max_keepalive_connections=0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _vetted_ip(self, host: str, port: int) -> str:
        try:
            addresses = await self._resolver(host, port)
        except OSError as exc:
            raise FetchRefused(f"could not resolve {host}") from exc
        if not addresses:
            raise FetchRefused(f"{host} has no addresses")
        parsed = [ipaddress.ip_address(a.split("%")[0]) for a in addresses]
        if not all(self._policy.address_ok(a) for a in parsed):
            raise FetchRefused(f"{host} resolves to a non-public address")
        return str(parsed[0])

    async def fetch(self, url: str, *, site: str | None = None) -> FetchResult:
        """GET one page under every guard; follows at most MAX_REDIRECTS on the same site."""
        deadline = time.monotonic() + self._policy.total_timeout_s
        current = url
        home = site or _site(urlsplit(url).hostname or "")
        for _ in range(MAX_REDIRECTS + 1):
            response_url, status, headers, body = await self._fetch_once(current, deadline, home)
            if status in (301, 302, 303, 307, 308) and "location" in headers:
                current = urljoin(response_url, headers["location"])
                continue
            content_type = headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type not in ALLOWED_TYPES:
                raise FetchRefused(f"unsupported content type {content_type or 'missing'}")
            charset = _charset(headers.get("content-type", ""))
            return FetchResult(
                url=response_url,
                status=status,
                content_type=content_type,
                text=_decode(body, charset),
            )
        raise FetchRefused("too many redirects")

    async def _fetch_once(
        self, url: str, deadline: float, home: str
    ) -> tuple[str, int, dict[str, str], bytes]:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https":
            raise FetchRefused("only https URLs are fetched")
        if parts.username or parts.password:
            raise FetchRefused("URLs with credentials are refused")
        if not host or _site(host) != home:
            raise FetchRefused(f"{host or 'empty host'} is not on {home}")
        port = parts.port or 443
        if port not in self._policy.ports:
            raise FetchRefused(f"port {port} is not allowed")
        ip = await self._vetted_ip(host, port)
        netloc = f"[{ip}]" if ":" in ip else ip
        target = urlunsplit(("https", f"{netloc}:{port}", parts.path or "/", parts.query, ""))
        request = self._client.build_request(
            "GET",
            target,
            headers={"Host": host if port == 443 else f"{host}:{port}"},
            extensions={"sni_hostname": host},  # TLS checks the certificate against `host`
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchRefused("total time limit reached")
        try:
            async with asyncio.timeout(remaining):
                response = await self._client.send(request, stream=True)
                try:
                    body = await self._read_capped(response)
                finally:
                    await response.aclose()
        except TimeoutError as exc:
            raise FetchRefused("total time limit reached mid-download") from exc
        except (httpx.ConnectError, httpx.ReadError, httpx.ProtocolError) as exc:
            raise FetchRefused(f"connection to {host} failed: {type(exc).__name__}") from exc
        public_url = urlunsplit(
            ("https", host if port == 443 else f"{host}:{port}", parts.path or "/", parts.query, "")
        )
        headers = {k.lower(): v for k, v in response.headers.items()}
        return public_url, response.status_code, headers, body

    async def _read_capped(self, response: httpx.Response) -> bytes:
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self._policy.max_body_bytes:
            raise FetchRefused("page is larger than the size cap")
        # Compressed bodies are refused outright: a small compressed chunk can expand far past
        # the cap before a per-chunk check sees it.
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in ("", "identity"):
            raise FetchRefused(f"compressed responses are refused ({encoding})")
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_raw():
            size += len(chunk)
            if size > self._policy.max_body_bytes:
                raise FetchRefused("page exceeded the size cap mid-download")
            chunks.append(chunk)
        return b"".join(chunks)


def _decode(body: bytes, charset: str) -> str:
    try:
        return body.decode(charset, errors="replace")
    except LookupError:  # a charset Python doesn't know
        return body.decode("utf-8", errors="replace")


def _charset(content_type: str) -> str:
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value:
            return value.strip("\"'") or "utf-8"
    return "utf-8"
