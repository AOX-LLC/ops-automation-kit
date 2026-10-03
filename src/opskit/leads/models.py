"""What one company's research produces: verified fields with their sources, and findings."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

FIELDS = ("domain", "industry", "employee_band", "hq_city", "founded_year", "description")

type FieldName = Literal[
    "domain", "industry", "employee_band", "hq_city", "founded_year", "description"
]
FindingKind = Literal["citation_rejected", "conflict", "unsupported_value", "domain_mismatch"]
ResearchStatus = Literal["researched", "unresolved"]


class FieldValue(BaseModel):
    """A field that survived verification: the quote is in the cited document's text."""

    value: str | int
    source_url: str
    quote: str


class Finding(BaseModel):
    field: str
    kind: FindingKind
    detail: str


class ResearchOutcome(BaseModel):
    company_name: str
    city_hint: str
    website: str | None = None
    domain: str | None = None
    status: ResearchStatus
    reason: str | None = None  # why it is unresolved, e.g. "no website given"
    fields: dict[str, FieldValue | None]
    findings: list[Finding] = []
    pages: list[str] = []  # documents the model was shown
    raw_cites: int = 0  # citations the model returned, before the repo's checks
    valid_cites: int = 0  # of those, how many passed
    replay_key: str | None = None
    cost_usd: str = "0"
    latency_ms: int | None = None


def empty_fields() -> dict[str, FieldValue | None]:
    return {name: None for name in FIELDS}
