"""Block commits that mention names from a local, gitignored denylist.

The denylist lives in `.denylist.local` (one term per line, `#` for comments) and
never enters the repository. Without it, the hook is a no-op.
"""

from __future__ import annotations

import sys
from pathlib import Path

DENYLIST = Path(__file__).resolve().parents[1] / ".denylist.local"


def load_terms() -> list[str]:
    if not DENYLIST.is_file():
        return []
    lines = DENYLIST.read_text(encoding="utf-8").splitlines()
    return [line.strip().lower() for line in lines if line.strip() and not line.startswith("#")]


def find_hits(path: Path, terms: list[str]) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return []
    return [term for term in terms if term in text]


def main(paths: list[str]) -> int:
    terms = load_terms()
    failed = False
    for name in paths:
        hits = find_hits(Path(name), terms)
        if hits:
            failed = True
            # Print the file only: echoing the term would leak it into CI or terminal logs.
            print(f"{name}: contains {len(hits)} denylisted term(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
