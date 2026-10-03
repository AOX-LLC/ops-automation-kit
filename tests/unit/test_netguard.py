"""The live-research fetcher's guards, against a local HTTPS server with test certificates."""

from __future__ import annotations

import ipaddress
import shutil
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from opskit.leads import netguard
from opskit.leads.netguard import FetchPolicy, FetchRefused, GuardedFetcher, is_public

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("93.184.216.34", True),
        ("2606:4700::1111", True),
        ("10.0.0.5", False),
        ("172.16.3.4", False),
        ("192.168.1.1", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("0.0.0.0", False),  # noqa: S104 - an address under test, not a bind
        ("224.0.0.1", False),
        ("::1", False),
        ("fe80::1", False),
        ("fc00::1", False),
        ("::ffff:10.0.0.1", False),
        ("::ffff:127.0.0.1", False),
    ],
)
def test_is_public(address: str, public: bool) -> None:
    assert is_public(ipaddress.ip_address(address)) is public


def _openssl(*args: str, cwd: Path) -> None:
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def certs(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("certs")
    _openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "1",
        "-subj",
        "/CN=test-ca",
        "-keyout",
        "ca.key",
        "-out",
        "ca.pem",
        cwd=d,
    )
    for name in ("right.test", "wrong.test"):
        _openssl(
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            f"/CN={name}",
            "-keyout",
            f"{name}.key",
            "-out",
            f"{name}.csr",
            cwd=d,
        )
        (d / f"{name}.ext").write_text(f"subjectAltName=DNS:{name}\n")
        _openssl(
            "x509",
            "-req",
            "-in",
            f"{name}.csr",
            "-CA",
            "ca.pem",
            "-CAkey",
            "ca.key",
            "-CAcreateserial",
            "-days",
            "1",
            "-extfile",
            f"{name}.ext",
            "-out",
            f"{name}.pem",
            cwd=d,
        )
    return d


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        return None

    def do_GET(self) -> None:
        if self.path == "/ok":
            body = b"<html><body>Acme Plumbing, founded 1999.</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/big":  # no Content-Length: the cap must trip while streaming
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            for _ in range(64):
                self.wfile.write(b"x" * 16384)
        elif self.path == "/slow":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            for _ in range(20):
                self.wfile.write(b"y" * 100)
                self.wfile.flush()
                time.sleep(0.3)
        elif self.path == "/offsite":
            self.send_response(302)
            self.send_header("Location", "https://evil.example/steal")
            self.end_headers()
        elif self.path == "/gzip":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", "3")
            self.end_headers()
            self.wfile.write(b"abc")
        elif self.path == "/odd-charset":
            body = b"<p>Acme</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=x-no-such-charset")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/subdomain":
            self.send_response(302)
            self.send_header("Location", "https://other.right.test/")
            self.end_headers()
        elif self.path.startswith("/status/"):
            code = int(self.path.rsplit("/", 1)[1])
            body = b'{"error": "nope"}'
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/hop/"):  # /hop/N redirects to /hop/N-1, then to /ok
            remaining = int(self.path.rsplit("/", 1)[1])
            self.send_response(302)
            self.send_header("Location", f"/hop/{remaining - 1}" if remaining else "/ok")
            self.end_headers()
        elif self.path == "/echo-agent":
            body = self.headers.get("User-Agent", "").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/binary":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", "3")
            self.end_headers()
            self.wfile.write(b"abc")


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request: object, client_address: object) -> None:
        """The fetcher hangs up mid-stream on purpose (caps, deadlines); that is not an error."""


class KeepAliveHandler(Handler):
    protocol_version = "HTTP/1.1"  # keeps the connection open, so a client could reuse it


def _serve(
    certs: Path, name: str, handler: type[BaseHTTPRequestHandler] = Handler
) -> Iterator[int]:
    server = QuietServer(("127.0.0.1", 0), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certs / f"{name}.pem", certs / f"{name}.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()


@pytest.fixture
def right_server(certs: Path) -> Iterator[int]:
    yield from _serve(certs, "right.test")


@pytest.fixture
def wrong_server(certs: Path) -> Iterator[int]:
    yield from _serve(certs, "wrong.test")


@pytest.fixture
def keepalive_server(certs: Path) -> Iterator[int]:
    yield from _serve(certs, "right.test", KeepAliveHandler)


def _fetcher(certs: Path, port: int, *, total: float = 3.0) -> GuardedFetcher:
    async def loopback(host: str, p: int) -> Sequence[str]:
        return ["127.0.0.1"]

    policy = FetchPolicy(
        address_ok=lambda a: a.is_loopback,  # tests only: the production policy is is_public
        ports=frozenset({port}),
        total_timeout_s=total,
        max_body_bytes=256 * 1024,
    )
    return GuardedFetcher(
        policy=policy,
        resolver=loopback,
        ssl_context=ssl.create_default_context(cafile=str(certs / "ca.pem")),
    )


async def test_valid_certificate_for_the_hostname_is_accepted(
    certs: Path, right_server: int
) -> None:
    fetcher = _fetcher(certs, right_server)
    try:
        result = await fetcher.fetch(f"https://right.test:{right_server}/ok")
    finally:
        await fetcher.aclose()
    assert "founded 1999" in result.text
    assert result.url == f"https://right.test:{right_server}/ok"


async def test_certificate_for_another_name_is_refused(certs: Path, wrong_server: int) -> None:
    # Connecting to the vetted IP must still verify the certificate against the hostname.
    fetcher = _fetcher(certs, wrong_server)
    try:
        with pytest.raises(FetchRefused, match=r"connection to right\.test failed"):
            await fetcher.fetch(f"https://right.test:{wrong_server}/ok")
    finally:
        await fetcher.aclose()


async def test_size_cap_aborts_mid_download(certs: Path, right_server: int) -> None:
    fetcher = _fetcher(certs, right_server)
    try:
        with pytest.raises(FetchRefused, match="size cap mid-download"):
            await fetcher.fetch(f"https://right.test:{right_server}/big")
    finally:
        await fetcher.aclose()


async def test_total_deadline_aborts_a_trickling_download(certs: Path, right_server: int) -> None:
    fetcher = _fetcher(certs, right_server, total=1.0)
    started = time.monotonic()
    try:
        with pytest.raises(FetchRefused, match="time limit"):
            await fetcher.fetch(f"https://right.test:{right_server}/slow")
    finally:
        await fetcher.aclose()
    assert time.monotonic() - started < 2.5  # stopped near the deadline, not after 6 s


async def test_redirect_off_site_is_refused(certs: Path, right_server: int) -> None:
    fetcher = _fetcher(certs, right_server)
    try:
        with pytest.raises(FetchRefused, match=r"is not on right\.test"):
            await fetcher.fetch(f"https://right.test:{right_server}/offsite")
    finally:
        await fetcher.aclose()


async def test_non_text_content_is_refused(certs: Path, right_server: int) -> None:
    fetcher = _fetcher(certs, right_server)
    try:
        with pytest.raises(FetchRefused, match="unsupported content type"):
            await fetcher.fetch(f"https://right.test:{right_server}/binary")
    finally:
        await fetcher.aclose()


async def test_private_resolution_is_refused_with_the_production_policy() -> None:
    async def private(host: str, port: int) -> Sequence[str]:
        return ["10.1.2.3"]

    fetcher = GuardedFetcher(resolver=private)
    try:
        with pytest.raises(FetchRefused, match="non-public address"):
            await fetcher.fetch("https://acme.example/about")
    finally:
        await fetcher.aclose()


async def test_any_private_address_among_several_is_refused() -> None:
    async def mixed(host: str, port: int) -> Sequence[str]:
        return ["93.184.216.34", "127.0.0.1"]

    fetcher = GuardedFetcher(resolver=mixed)
    try:
        with pytest.raises(FetchRefused, match="non-public address"):
            await fetcher.fetch("https://acme.example/")
    finally:
        await fetcher.aclose()


@pytest.mark.parametrize(
    "url", ["http://acme.example/", "https://user:pw@acme.example/", "https://acme.example:8443/"]
)
async def test_scheme_credentials_and_ports_are_refused(url: str) -> None:
    fetcher = GuardedFetcher()
    try:
        with pytest.raises(FetchRefused):
            await fetcher.fetch(url)
    finally:
        await fetcher.aclose()


async def test_a_connection_is_never_reused_for_another_hostname(
    certs: Path, keepalive_server: int
) -> None:
    # Both names resolve to the same IP; the second one's certificate must still be checked.
    fetcher = _fetcher(certs, keepalive_server)
    try:
        await fetcher.fetch(f"https://right.test:{keepalive_server}/ok")
        with pytest.raises(FetchRefused, match=r"connection to wrong\.test failed"):
            await fetcher.fetch(f"https://wrong.test:{keepalive_server}/ok")
    finally:
        await fetcher.aclose()


async def test_a_compressed_response_is_refused(certs: Path, right_server: int) -> None:
    fetcher = _fetcher(certs, right_server)
    try:
        with pytest.raises(FetchRefused, match="compressed"):
            await fetcher.fetch(f"https://right.test:{right_server}/gzip")
    finally:
        await fetcher.aclose()


async def test_an_unknown_charset_falls_back_to_utf8(certs: Path, right_server: int) -> None:
    fetcher = _fetcher(certs, right_server)
    try:
        result = await fetcher.fetch(f"https://right.test:{right_server}/odd-charset")
    finally:
        await fetcher.aclose()
    assert "Acme" in result.text


async def test_a_redirect_to_a_sibling_subdomain_is_refused(certs: Path, right_server: int) -> None:
    fetcher = _fetcher(certs, right_server)
    try:
        with pytest.raises(FetchRefused, match=r"other\.right\.test is not on right\.test"):
            await fetcher.fetch(f"https://right.test:{right_server}/subdomain")
    finally:
        await fetcher.aclose()


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("www.acme.example", "acme.example", True),
        ("evil.co.uk", "victim.co.uk", False),
        ("evil.github.io", "victim.github.io", False),
    ],
)
def test_site_is_the_host_less_www(a: str, b: str, same: bool) -> None:
    assert (netguard._site(a) == netguard._site(b)) is same


@pytest.mark.parametrize("code", [403, 404, 429, 500, 503])
async def test_an_error_status_is_returned_not_raised(
    certs: Path, right_server: int, code: int
) -> None:
    # The body (here JSON, an unsupported type) is never read for a non-2xx answer.
    fetcher = _fetcher(certs, right_server)
    try:
        result = await fetcher.fetch(f"https://right.test:{right_server}/status/{code}")
    finally:
        await fetcher.aclose()
    assert (result.status, result.text) == (code, "")


async def test_three_redirects_are_followed_and_a_fourth_is_refused(
    certs: Path, right_server: int
) -> None:
    fetcher = _fetcher(certs, right_server)
    try:
        ok = await fetcher.fetch(f"https://right.test:{right_server}/hop/2")  # 3 redirects
        assert "founded 1999" in ok.text
        with pytest.raises(FetchRefused, match="too many redirects"):
            await fetcher.fetch(f"https://right.test:{right_server}/hop/3")  # 4 redirects
    finally:
        await fetcher.aclose()


async def test_the_user_agent_identifies_the_kit(certs: Path, right_server: int) -> None:
    fetcher = _fetcher(certs, right_server)
    try:
        result = await fetcher.fetch(f"https://right.test:{right_server}/echo-agent")
    finally:
        await fetcher.aclose()
    assert result.text == netguard.USER_AGENT
    assert result.text.startswith("ops-automation-kit/")
