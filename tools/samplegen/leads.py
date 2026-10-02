"""Leads sample data: company list, research corpus, demo CRM and the answer key."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any

import yaml

from tools.samplegen.common import answer_key_dir, samples_dir, write_json, write_text

SPEC_PATH = Path(__file__).parent / "specs" / "leads.yaml"
RESEARCHED_FIELDS = (
    "domain",
    "industry",
    "employee_band",
    "hq_city",
    "founded_year",
    "description",
)
CRM_COLUMNS = ("name", "domain", "industry", "employee_band", "hq_city", "description")
PLACEHOLDER = re.compile(r"\[\[([a-z_.]+)\]\]")


@dataclass(frozen=True)
class Doc:
    id: str
    host: str
    kind: str
    title: str
    body: tuple[str, ...]
    supports: tuple[str, ...]
    claims: dict[str, str]

    @property
    def extension(self) -> str:
        return "txt" if self.kind == "dump" else "html"

    def corpus_path(self) -> str:
        return f"samples/leads/corpus/{self.host}/{self.id}.{self.extension}"


@dataclass
class Spec:
    companies: dict[str, dict[str, Any]]
    docs: list[Doc]
    crm_accounts: list[dict[str, str]]
    host_domains: dict[str, str] = field(default_factory=dict)


def load_spec(path: Path = SPEC_PATH) -> Spec:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    companies: dict[str, dict[str, Any]] = raw["companies"]
    for slug, company in companies.items():
        company["domain"] = f"{slug}.example"
    domains = {slug: company["domain"] for slug, company in companies.items()}
    docs = [_parse_doc(entry, domains) for entry in raw["docs"]]
    spec = Spec(companies, docs, raw["crm_accounts"], domains)
    _check_unique_paths(docs)
    return spec


def _parse_doc(entry: dict[str, Any], domains: dict[str, str]) -> Doc:
    return Doc(
        id=entry["id"],
        host=domains.get(entry["host"], entry["host"]),
        kind=entry["kind"],
        title=entry["title"],
        body=tuple(entry["body"]),
        supports=tuple(entry.get("supports", [])),
        claims=dict(entry.get("claims", {})),
    )


def _check_unique_paths(docs: list[Doc]) -> None:
    paths = [doc.corpus_path() for doc in docs]
    if len(paths) != len(set(paths)):
        raise ValueError("duplicate corpus path in leads.yaml")


def _default_slug(doc: Doc, spec: Spec) -> str:
    for slug, domain in spec.host_domains.items():
        if domain == doc.host:
            return slug
    return ""


def _fill(text: str, doc: Doc, spec: Spec) -> tuple[str, set[str]]:
    used: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        slug, _, name = key.rpartition(".")
        slug = slug or _default_slug(doc, spec)
        used.add(f"{slug}.{name}")
        return str(spec.companies[slug][name])

    return PLACEHOLDER.sub(replace, text), used


def render_paragraphs(doc: Doc, spec: Spec) -> list[str]:
    filled: list[str] = []
    used: set[str] = set()
    for paragraph in doc.body:
        text, found = _fill(paragraph, doc, spec)
        filled.append(text)
        used |= found
    _validate_support(doc, used, "\n".join(filled))
    return filled


def _validate_support(doc: Doc, used: set[str], text: str) -> None:
    missing = set(doc.supports) - used
    if missing:
        raise ValueError(f"{doc.corpus_path()} declares unbacked support: {sorted(missing)}")
    for fact, value in doc.claims.items():
        if value not in text:
            raise ValueError(f"{doc.corpus_path()} claim {fact} not in body")


def _is_list(paragraph: str) -> bool:
    return all(line.startswith("* ") for line in paragraph.split("\n"))


def _html_block(paragraph: str) -> str:
    if _is_list(paragraph):
        items = "\n".join(
            f"    <li>{escape(line[2:], quote=False)}</li>" for line in paragraph.split("\n")
        )
        return f"  <ul>\n{items}\n  </ul>"
    return f"  <p>{escape(paragraph, quote=False)}</p>"


def render_html(doc: Doc, paragraphs: list[str]) -> str:
    title = escape(doc.title, quote=False)
    blocks = "\n".join(_html_block(paragraph) for paragraph in paragraphs)
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"  <title>{title}</title>\n</head>\n<body>\n<main>\n  <h1>{title}</h1>\n{blocks}\n"
        "</main>\n</body>\n</html>\n"
    )


def render_text(doc: Doc, paragraphs: list[str]) -> str:
    return f"{doc.title}\n{'=' * len(doc.title)}\n\n" + "\n\n".join(paragraphs) + "\n"


def render_doc(doc: Doc, spec: Spec) -> str:
    paragraphs = render_paragraphs(doc, spec)
    return render_text(doc, paragraphs) if doc.extension == "txt" else render_html(doc, paragraphs)


def csv_text(header: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def companies_csv(spec: Spec) -> str:
    rows = sorted((c["name"], c["city_hint"]) for c in spec.companies.values())
    return csv_text(("company_name", "city_hint"), rows)


def accounts_csv(spec: Spec) -> str:
    rows = sorted(tuple(account[col] for col in CRM_COLUMNS) for account in spec.crm_accounts)
    return csv_text(CRM_COLUMNS, rows)


def _fact_docs(spec: Spec, slug: str, name: str) -> list[tuple[Doc, str]]:
    """Return (doc, stated value) for every doc that states this company field, in spec order."""
    fact = f"{slug}.{name}"
    found: list[tuple[Doc, str]] = []
    for doc in spec.docs:
        if fact in doc.claims:
            found.append((doc, doc.claims[fact]))
        elif fact in doc.supports:
            found.append((doc, spec.companies[slug][name]))
    return found


def _field_record(spec: Spec, slug: str, name: str) -> dict[str, Any]:
    found = _fact_docs(spec, slug, name)
    if not found:
        return {"value": None, "source": None}
    if name in spec.companies[slug].get("conflicts", []):
        candidates = [{"value": value, "source": doc.corpus_path()} for doc, value in found]
        return {"value": None, "source": None, "conflict": True, "candidates": candidates}
    doc, value = found[0]
    return {"value": value, "source": doc.corpus_path()}


def _crm_action(spec: Spec, slug: str) -> str:
    crm_domains = {account["domain"] for account in spec.crm_accounts}
    return "update" if spec.companies[slug]["domain"] in crm_domains else "create"


def _company_record(spec: Spec, slug: str) -> dict[str, Any]:
    company = spec.companies[slug]
    return {
        "city_hint": company["city_hint"],
        "crm_action": _crm_action(spec, slug),
        "fields": {name: _field_record(spec, slug, name) for name in RESEARCHED_FIELDS},
        "notes": company.get("notes", ""),
    }


def expected_records(spec: Spec) -> dict[str, Any]:
    return {c["name"]: _company_record(spec, slug) for slug, c in sorted(spec.companies.items())}


def generate(out_root: Path) -> None:
    spec = load_spec()
    base = samples_dir(out_root, "leads")
    write_text(base / "companies.csv", companies_csv(spec))
    for doc in spec.docs:
        write_text(out_root / doc.corpus_path(), render_doc(doc, spec))
    write_text(samples_dir(out_root, "crm") / "accounts.csv", accounts_csv(spec))
    write_json(answer_key_dir(out_root, "leads") / "expected_records.json", expected_records(spec))
