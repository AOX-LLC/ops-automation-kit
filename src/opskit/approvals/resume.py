"""Guard and sender for n8n resume URLs.

A resume URL is a bearer capability: n8n signs it, and whoever holds it can resume the
execution. The helper accepts only URLs pointing at the kit's own n8n waiting-webhook path,
and always delivers to n8n's internal address, so a stored URL cannot be aimed elsewhere.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx

from opskit.config import Settings

WAITING_PATH = re.compile(r"^/webhook-waiting/[0-9]{1,18}$")
SIGNATURE = re.compile(r"^[0-9a-f]{16,128}$")
TIMEOUT = httpx.Timeout(5.0)


class InvalidResumeUrl(ValueError):
    """The URL is not a signed waiting-webhook URL from this kit's n8n."""


def _origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return parts.scheme, parts.netloc.lower()


def internal_resume_target(url: str, settings: Settings) -> str:
    """Validate a resume URL from n8n and return the internal URL to deliver to."""
    parts = urlsplit(url)
    allowed_origins = {_origin(settings.n8n_public_url), _origin(settings.n8n_internal_url)}
    if (parts.scheme, parts.netloc.lower()) not in allowed_origins:
        raise InvalidResumeUrl("resume URL must point at this kit's n8n")
    if parts.username or parts.password or parts.fragment:
        raise InvalidResumeUrl("resume URL must not carry credentials or a fragment")
    if not WAITING_PATH.fullmatch(parts.path):
        raise InvalidResumeUrl("resume URL must be an n8n waiting-webhook path")
    try:
        query = parse_qs(parts.query, strict_parsing=True)
    except ValueError as exc:
        raise InvalidResumeUrl("resume URL has a malformed query") from exc
    signatures = query.get("signature", [])
    if (
        set(query) != {"signature"}
        or len(signatures) != 1
        or not SIGNATURE.fullmatch(signatures[0])
    ):
        raise InvalidResumeUrl("resume URL must carry exactly one n8n signature")
    internal = urlsplit(settings.n8n_internal_url)
    return urlunsplit((internal.scheme, internal.netloc, parts.path, parts.query, ""))


class N8nResumeSender:
    """Posts a decision to the waiting execution. Never follows redirects."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False)

    async def __call__(self, stored_url: str, payload: dict[str, Any]) -> int:
        target = internal_resume_target(stored_url, self._settings)
        response = await self._client.post(target, json=payload)
        return response.status_code

    async def aclose(self) -> None:
        await self._client.aclose()
