"""Where a company's text comes from: the local corpus (default) or its own public site.

Both return `Document`s whose `url` is what the model cites, and what the repo later checks
a quote against. The corpus is fictional fixture data used in replay and record. The web
retriever is live only: it fetches the company's own site through the guarded fetcher,
asks robots.txt first, and reads at most five pages from a fixed path list.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from opskit.leads.netguard import FetchRefused, GuardedFetcher
from opskit.leads.robots import RobotsCache

MAX_DOC_CHARS = 20_000
CUT_MARGIN = 256
PAGE_PATHS = (
    "/",
    "/about",
    "/about-us",
    "/company",
    "/contact",
)  # fixed: never followed from links
HOSTNAME = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_SKIPPED = frozenset({"script", "style", "noscript", "template", "svg"})
_BLOCKS = frozenset(
    {"p", "div", "li", "ul", "ol", "br", "tr", "section", "article", "main", "header", "footer"}
    | {"h1", "h2", "h3", "h4", "h5", "h6", "title", "td", "th", "dd", "dt"}
)


@dataclass(frozen=True, slots=True)
class Company:
    name: str
    city_hint: str
    website: str | None = None


@dataclass(frozen=True, slots=True)
class Document:
    url: str
    text: str
    own: bool  # the company's own site, as opposed to a third-party listing


@dataclass(slots=True)
class Retrieval:
    documents: list[Document] = field(default_factory=list)
    unresolved_reason: str | None = None
    notes: list[str] = field(default_factory=list)  # what was fetched, skipped and why


class Retriever(Protocol):
    async def fetch(self, company: Company) -> Retrieval: ...


def normalize_website(raw: str | None) -> str | None:
    """The bare host ("acme.example") from "acme.example", "https://www.acme.example/", or None.

    Ports, credentials, IP literals and anything that isn't a plain DNS name are rejected.
    """
    if raw is None or not raw.strip():
        return None
    text = raw.strip().lower()
    parts = urlsplit(text if "//" in text else f"//{text}")
    if parts.scheme not in ("", "http", "https") or parts.username or parts.password:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").rstrip(".").removeprefix("www.")
    if port is not None or not HOSTNAME.fullmatch(host):
        return None
    return host


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED:
            self._skip += 1
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str, limit: int = MAX_DOC_CHARS) -> str:
    """Visible text only: scripts and styles dropped, capped at `limit` characters."""
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    lines = (" ".join(line.split()) for line in "".join(extractor.parts).split("\n"))
    return "\n".join(line for line in lines if line)[:limit]


# Contact details: email addresses (plain and spelled out) and phone numbers in the usual
# shapes. Live pages have them redacted before the model sees the text, and a quote that
# still carries one, or touches a redacted span, is refused (see extraction.py). Every
# quantifier is bounded and the local part of an address is anchored, so a hostile page of
# 20,000 "a" characters cannot make the match quadratic.
_LOCAL = r"(?<![\w.+-])[\w.+-]{1,64}"
_HOST = r"[\w-]{1,63}"
REDACTED = "[contact details removed]"
_CUES = (
    "call|phone|telephone|tel|fax|mobile|cell|text|dial|whatsapp|sms|contact|reach|orders|questions"
)
CONTACT_DETAILS = re.compile(
    rf"{re.escape(REDACTED)}"
    rf"|{_LOCAL}\s{{0,3}}@\s{{0,3}}{_HOST}(?:\.{_HOST}){{0,6}}\.[a-z]{{2,24}}"  # jane@host.com
    rf"|{_LOCAL}\s{{0,3}}(?:\[at\]|\(at\)|\{{at\}})\s{{0,3}}{_HOST}"  # jane [at] host
    rf"|{_LOCAL}\s{{1,3}}at\s{{1,3}}{_HOST}\s{{1,3}}(?:dot|\[dot\]|\(dot\))\s{{1,3}}[a-z]{{2,24}}"
    rf"|(?:e-?mail|mail|write|contact|reach)\b\W{{0,12}}{_LOCAL}\s{{1,3}}at\s{{1,3}}{_HOST}\.[a-z]{{2,24}}"
    r"|\+\d[\d\s().-]{6,20}\d"  # +44 20 7946 0958
    r"|(?<![\w-])\d{10,}(?!\d)"  # 5550100142
    r"|(?<!\d)\(?\d{3}\)?[ \t\n.\-/]{1,3}\d{3}[ \t\n.\-/]{1,3}\d{4}(?!\d)"  # 555 - 010 - 0142
    r"|(?<!\d)\(\d{3}\)[ \t\n.\-/]{0,3}\d{3}[ \t\n.\-/]{1,3}\d{4}(?!\d)"  # (555)010-0142
    r"|(?<!\d)0\d{2,4}[ -]\d{5,8}(?!\d)"  # 07700 900123, 030 12345678
    r"|(?<!\d)0\d{2,4}[ -]\d{3,4}[ -]\d{3,4}(?!\d)"  # 020 7946 0958
    rf"|(?<![a-z])(?:{_CUES})\b[^\d\n]{{0,25}}\d{{3}}[ .\-]?\d{{4}}(?!\d)",
    re.I,
)
# A bare seven-digit number: 555-0142, 212-3456, 800-5000. It is a phone number unless it
# reads as a count or a price: a round number that climbs, and is followed by what is being
# counted ("501-1000 employees", "250-1000 homes"). Digits alone cannot tell the two apart
# (212-1000 is a real number), so a range with no unit is treated as a phone number.
_COUNTED = (
    r"employees?|staff|people|workers|team members|members|homes|customers|clients|jobs|units"
    r"|users|projects|vehicles|locations|offices|sq|square|per"
)
_LOCAL_NUMBER = re.compile(
    rf"(?<![\d$-])(\d{{3}})[ \t\n.-](\d{{4}})(?![\d-])(\s*(?:{_COUNTED})\b)?", re.I
)


def _is_local_number(match: re.Match[str]) -> bool:
    first, second, counted = match.groups()
    round_and_climbing = second.endswith("00") and int(second) > int(first)
    return not (round_and_climbing and counted)


# Dashes and spaces that are not the plain ASCII ones, mapped to the plain ones.
_DASHES_AND_SPACES = {
    **dict.fromkeys((0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212), "-"),
    **dict.fromkeys((0xB7, 0x2027, 0x2219, 0x2E3A, 0x2E3B), "-"),
    **dict.fromkeys((0xA0, 0x2009, 0x202F), " "),
}
# Invisible code points that are not format characters (category Cf) but render as nothing.
_INVISIBLE = {0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x3164, 0xFFA0}
_INVISIBLE |= set(range(0x180B, 0x1810)) | set(range(0xFE00, 0xFE10))
_INVISIBLE |= set(range(0xE0100, 0xE01F0))
# For matching a quote against a page: also curly quotes.
_UNIFY = {
    **_DASHES_AND_SPACES,
    **dict.fromkeys((0x2018, 0x2019), "'"),
    **dict.fromkeys((0x201C, 0x201D), '"'),
}


def visible(text: str) -> str:
    """The text as a reader sees it: invisible characters removed, NFKC folded, and typographic
    dashes and spaces made plain."""
    shown = "".join(c for c in text if unicodedata.category(c) != "Cf" and ord(c) not in _INVISIBLE)
    return unicodedata.normalize("NFKC", shown).translate(_DASHES_AND_SPACES)


def has_contact_details(text: str) -> bool:
    seen = visible(text)
    if CONTACT_DETAILS.search(seen):
        return True
    return any(_is_local_number(m) for m in _LOCAL_NUMBER.finditer(seen))


def redact_contact_details(text: str) -> str:
    seen = CONTACT_DETAILS.sub(REDACTED, visible(text))
    return _LOCAL_NUMBER.sub(lambda m: REDACTED if _is_local_number(m) else m.group(0), seen)


def normalize(text: str) -> str:
    """Case-folded, dashes and quotes unified, whitespace collapsed (for matching only)."""
    return " ".join(text.translate(_UNIFY).casefold().split())


class CorpusRetriever:
    """Reads samples/leads/corpus/<domain>/: the company's own pages, plus any third-party
    listing (a directory or chamber page) that names the company in its city."""

    def __init__(self, root: Path, company_domains: frozenset[str]) -> None:
        self._root = root
        self._company_domains = company_domains

    async def fetch(self, company: Company) -> Retrieval:
        domain = normalize_website(company.website)
        if domain is None:
            return Retrieval(unresolved_reason="no website given")
        own = self._read_dir(domain, own=True)
        if not own:
            return Retrieval(unresolved_reason="no pages found for that website")
        name, city = normalize(company.name), normalize(company.city_hint)
        listings = [
            doc
            for host_dir in sorted(self._root.iterdir())
            if host_dir.is_dir() and host_dir.name not in self._company_domains
            for doc in self._read_dir(host_dir.name, own=False)
            if name in normalize(doc.text) and city in normalize(doc.text)
        ]
        documents = [*own, *listings]
        return Retrieval(documents=documents, notes=[f"read {doc.url}" for doc in documents])

    def _read_dir(self, host: str, *, own: bool) -> list[Document]:
        folder = self._root / host
        if not folder.is_dir():
            return []
        docs: list[Document] = []
        for path in sorted(folder.iterdir()):
            if path.suffix not in (".html", ".txt") or not path.is_file():
                continue
            raw = path.read_text(encoding="utf-8")
            text = html_to_text(raw) if path.suffix == ".html" else raw[:MAX_DOC_CHARS]
            docs.append(Document(url=f"corpus://{host}/{path.name}", text=text, own=own))
        return docs


class WebRetriever:
    """Live only. robots.txt first; then up to five fixed paths on the company's own site."""

    def __init__(self, fetcher: GuardedFetcher, robots: RobotsCache) -> None:
        self._fetcher = fetcher
        self._robots = robots

    async def fetch(self, company: Company) -> Retrieval:
        domain = normalize_website(company.website)
        if domain is None:
            reason = (
                "no website given" if not (company.website or "").strip() else "invalid website"
            )
            return Retrieval(unresolved_reason=reason)
        result = Retrieval()
        seen: set[str] = set()
        for path in PAGE_PATHS:
            url = f"https://{domain}{path}"
            decision = await self._robots.decision_for(url)
            if decision.blocked:
                result.notes.append(f"robots: {decision.reason}")
                result.unresolved_reason = "blocked by robots.txt"
                return result
            if not decision.allows(url):
                result.notes.append(f"robots: {path} disallowed")
                continue
            try:
                page = await self._fetcher.fetch(
                    url, allow=decision.allows
                )  # also on redirect hops
            except FetchRefused as exc:
                result.notes.append(f"{path}: refused ({exc})")
                continue
            if not 200 <= page.status < 300:
                result.notes.append(f"{path}: status {page.status}")
                continue
            if page.url in seen:
                result.notes.append(f"{path}: same page as an earlier path")
                continue
            seen.add(page.url)
            # Cut with a margin, redact, then cut: a contact detail that straddles the limit is
            # redacted whole instead of being left half-visible at the end.
            room = MAX_DOC_CHARS + CUT_MARGIN
            text = html_to_text(page.text, room) if page.content_type == "text/html" else page.text
            text = redact_contact_details(text[:room])[:MAX_DOC_CHARS]
            result.documents.append(Document(url=page.url, text=text, own=True))
            result.notes.append(f"{path}: fetched {page.url}")
        if not result.documents:
            result.unresolved_reason = "no pages could be read"
        return result
