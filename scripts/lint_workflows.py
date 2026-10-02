"""Check exported n8n workflows against the kit's rules.

Rejects Code and Execute Command nodes, inline credential data, pinned data, helper URLs
outside http://api:8000/, hardcoded localhost or IP URLs, and missing fixed ids.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

FORBIDDEN_NODE_TYPES = {
    "n8n-nodes-base.code",
    "n8n-nodes-base.function",
    "n8n-nodes-base.functionItem",
    "n8n-nodes-base.executeCommand",
}
HELPER_PREFIX = "http://api:8000/"
HARDCODED_HOST = re.compile(r"https?://(localhost|127\.0\.0\.1|0\.0\.0\.0|\d{1,3}(\.\d{1,3}){3})\b")
WORKFLOW_ID = re.compile(r"^[A-Za-z0-9]{16}$")


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return []


def check_node(node: dict[str, Any]) -> list[str]:
    name = node.get("name", "?")
    problems = []
    if node.get("type") in FORBIDDEN_NODE_TYPES:
        problems.append(f"node '{name}' is a {node['type']} node; logic belongs in the helper API")
    for credential in node.get("credentials", {}).values():
        if set(credential) - {"id", "name"}:
            problems.append(f"node '{name}' carries inline credential data")
    url = node.get("parameters", {}).get("url")
    if isinstance(url, str) and not url.lstrip("=").startswith(HELPER_PREFIX):
        problems.append(f"node '{name}' calls {url!r}; helper URLs must start with {HELPER_PREFIX}")
    for text in _strings(node.get("parameters", {})):
        if HARDCODED_HOST.search(text):
            problems.append(f"node '{name}' hardcodes a localhost or IP address")
    return problems


def check_workflow(data: dict[str, Any]) -> list[str]:
    problems = []
    if not WORKFLOW_ID.fullmatch(str(data.get("id", ""))):
        problems.append("workflow needs a fixed 16-character alphanumeric id")
    if data.get("pinData"):
        problems.append("workflow contains pinData; strip it before committing")
    for node in data.get("nodes", []):
        problems.extend(check_node(node))
    return problems


def main(paths: list[str]) -> int:
    files = [p for arg in paths for p in sorted(Path(arg).glob("*.json"))] or []
    failed = False
    for path in files:
        for problem in check_workflow(json.loads(path.read_text(encoding="utf-8"))):
            failed = True
            print(f"{path}: {problem}")
    print(f"checked {len(files)} workflow(s)")
    return 1 if failed or not files else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["n8n/workflows"]))
