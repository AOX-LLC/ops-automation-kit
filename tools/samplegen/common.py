"""Shared helpers that keep every generator's output byte-for-byte reproducible."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

SEED = 20261002
SAMPLE_DOMAIN_SUFFIX = ".example"
LABEL_FIELD_MARKERS = ("expected_", '"status"', '"category"', '"priority"')


def rng(stream: str) -> random.Random:
    """Return a generator-specific RNG so adding one generator never shifts another's output."""
    return random.Random(f"{SEED}:{stream}")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def samples_dir(out_root: Path, workflow: str) -> Path:
    return out_root / "samples" / workflow


def answer_key_dir(out_root: Path, workflow: str) -> Path:
    return out_root / "evals" / "answer_keys" / workflow
