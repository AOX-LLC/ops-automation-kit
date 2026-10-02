"""Content-addressed fixture keys and the on-disk fixture layout for mock mode.

A key depends only on what the model is asked: prompt id and version, tier, output schema,
inputs and attachment hashes. Run ids, n8n execution ids, timestamps, call order and the
model id never enter it, so parallel fan-out and re-runs replay identically.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from opskit.core.ports import Attachment, JsonValue, PromptRef, Tier


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def attachment_digests(attachments: Sequence[Attachment]) -> list[dict[str, str]]:
    return [
        {"media_type": item.media_type, "sha256": hashlib.sha256(item.data).hexdigest()}
        for item in attachments
    ]


def request_digest(
    *,
    prompt: PromptRef,
    tier: Tier,
    schema_name: str,
    inputs: Mapping[str, JsonValue],
    attachments: Sequence[Attachment],
) -> dict[str, Any]:
    return {
        "prompt_id": prompt.id,
        "prompt_version": prompt.version,
        "tier": tier.value,
        "schema": schema_name,
        "inputs": dict(inputs),
        "attachments": attachment_digests(attachments),
    }


def fixture_key(digest: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(digest).encode("utf-8")).hexdigest()


def fixture_path(root: Path, workflow: str, prompt_id: str, key: str) -> Path:
    return root / workflow / prompt_id / f"{key}.json"
