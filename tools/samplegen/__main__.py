"""Regenerate every sample dataset and answer key: python -m tools.samplegen [--out DIR]."""

from __future__ import annotations

import argparse
from pathlib import Path

from tools.samplegen import inbox, leads, receipts

REPO_ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=REPO_ROOT, help="root to write samples/ and evals/ into"
    )
    out_root: Path = parser.parse_args().out
    receipts.generate(out_root)
    leads.generate(out_root)
    inbox.generate(out_root)


if __name__ == "__main__":
    main()
