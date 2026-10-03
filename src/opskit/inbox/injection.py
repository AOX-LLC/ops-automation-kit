"""Deterministic pre-scan for prompt injection in inbound email, run before any model call.

Inbound email is untrusted input. This scan is one of two independent layers (the other is
the triage model's own judgement); either one quarantines the email. It looks for text
addressed to an assistant rather than to a business: instructions to ignore rules, role
changes, requests to export data, and instructions hidden behind invisible characters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Zero-width and bidi-control characters used to hide text from a human reader.
HIDDEN_CHARS = re.compile("[​‌‍⁠﻿‪-‮⁦-⁩]")
HIDDEN_RUN = 3
MAX_EVIDENCE = 160

PATTERNS: dict[str, re.Pattern[str]] = {
    "ignore_instructions": re.compile(
        r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b"
        r"(instructions?|prompts?|rules|guidelines|directions)\b",
        re.I,
    ),
    "role_change": re.compile(
        r"\b(you are now|act as|pretend to be|switch to|enter)\b[^.\n]{0,30}\b"
        r"(admin|developer|system|root|unrestricted|jailbreak)\b(\s+mode)?",
        re.I,
    ),
    "system_marker": re.compile(
        r"(^|\n)\s*(system|assistant|developer)\s*:|\bsystem prompt\b|\bdeveloper message\b",
        re.I,
    ),
    "data_export": re.compile(
        r"\b(forward|send|export|share|email|reply with|list|dump)\b[^.\n]{0,60}\b"
        r"(customer (list|data|records|contacts|phone numbers?)|all (customers|contacts|invoices|"
        r"emails|mail)|every customer'?s?|contact list|address book|(customer|client) database)\b",
        re.I,
    ),
    "fence_tag": re.compile(r"<\s*/?\s*(email|profile|system|instructions?)\b[^>]{0,40}>", re.I),
    "credential_request": re.compile(
        r"\b(reply with|send|give|share)\b[^.\n]{0,40}\b(password|api key|token|credentials?)\b",
        re.I,
    ),
}


@dataclass(frozen=True, slots=True)
class InjectionHit:
    rule: str
    evidence: str


def _evidence(text: str, start: int, end: int) -> str:
    snippet = " ".join(text[start:end].split())
    return snippet[:MAX_EVIDENCE]


def scan(text: str) -> list[InjectionHit]:
    """Every rule that matches, with a short normalised span as evidence."""
    hits: list[InjectionHit] = []
    hidden = HIDDEN_CHARS.findall(text)
    if len(hidden) >= HIDDEN_RUN:
        hits.append(InjectionHit("hidden_text", f"{len(hidden)} invisible characters"))
    visible = HIDDEN_CHARS.sub("", text)
    for rule, pattern in PATTERNS.items():
        match = pattern.search(visible)
        if match:
            hits.append(InjectionHit(rule, _evidence(visible, match.start(), match.end())))
    return hits
