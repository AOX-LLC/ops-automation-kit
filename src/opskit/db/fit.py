"""Cut a model-written list to what a bounded JSON column holds.

The database caps a JSON column by the size Postgres prints it at (see opskit.db.bounds), so a
list a model wrote has to be cut by bytes as well as by items, or a long one turns into a refused
insert. `json_bytes` measures a value the way jsonb prints it (raw UTF-8, `, ` between items).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any


def json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(", ", ": ")).encode())


def fit_list[T](
    items: Iterable[T], *, budget: int, max_items: int, item_chars: int | None = None
) -> list[T]:
    """The leading items that fit `budget` bytes (as an array's printed size) and `max_items`;
    each string is cut to `item_chars` first."""
    kept: list[T] = []
    used = 2  # the brackets
    for item in items:
        if len(kept) >= max_items:
            break
        if item_chars is not None and isinstance(item, str):
            item = item[:item_chars]  # type: ignore[assignment]
        size = json_bytes(item) + 2
        if used + size > budget:
            break
        kept.append(item)
        used += size
    return kept
