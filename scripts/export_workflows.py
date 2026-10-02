"""Normalize workflows exported from the n8n editor and write them over the committed files.

Usage: export_workflows.py <export_dir> <workflows_dir>
Prints "<workflow id> <sha256>" for each file written, for the import-state record.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

VOLATILE_KEYS = {
    "pinData",
    "versionId",
    "updatedAt",
    "createdAt",
    "triggerCount",
    "shared",
    "activeVersionId",
    "activeVersion",
    "isArchived",
    "parentFolderId",
}


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    clean = {key: value for key, value in data.items() if key not in VOLATILE_KEYS}
    clean.get("meta", {}).pop("instanceId", None)
    if clean.get("meta") == {}:
        clean.pop("meta")
    clean["active"] = False  # publishing is decided by the import script, not the export
    return clean


def committed_files_by_id(workflows_dir: Path) -> dict[str, Path]:
    return {
        json.loads(path.read_text(encoding="utf-8"))["id"]: path
        for path in sorted(workflows_dir.glob("*.json"))
    }


def main(export_dir: Path, workflows_dir: Path) -> None:
    targets = committed_files_by_id(workflows_dir)
    for exported in sorted(export_dir.glob("*.json")):
        data = normalize(json.loads(exported.read_text(encoding="utf-8")))
        target = targets.get(data["id"], workflows_dir / f"{data['id']}.json")
        text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        target.write_text(text, encoding="utf-8")
        print(data["id"], hashlib.sha256(text.encode("utf-8")).hexdigest())
        print(f"wrote {target}", file=sys.stderr)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
