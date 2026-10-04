import json
from pathlib import Path

import pytest

from scripts.lint_workflows import check_workflow

WORKFLOWS = sorted(Path("n8n/workflows").glob("*.json"))


def base(**extra: object) -> dict[str, object]:
    return {"id": "abcdefghij123456", "nodes": [], "connections": {}, **extra}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_committed_workflows_pass(path: Path) -> None:
    assert check_workflow(json.loads(path.read_text())) == []


def test_there_are_six_workflows() -> None:
    assert len(WORKFLOWS) == 6


def test_code_node_is_rejected() -> None:
    workflow = base(nodes=[{"name": "c", "type": "n8n-nodes-base.code", "parameters": {}}])
    assert any("Code" in p or "code" in p for p in check_workflow(workflow))


def test_inline_credential_data_is_rejected() -> None:
    node = {
        "name": "h",
        "type": "x",
        "parameters": {},
        "credentials": {"httpHeaderAuth": {"id": "a", "name": "b", "data": {"value": "s"}}},
    }
    assert check_workflow(base(nodes=[node]))


def test_pin_data_is_rejected() -> None:
    assert check_workflow(base(pinData={"Webhook": [{}]}))


def test_localhost_url_is_rejected() -> None:
    node = {
        "name": "h",
        "type": "n8n-nodes-base.httpRequest",
        "parameters": {"url": "http://localhost:4301/v1/runs"},
    }
    assert check_workflow(base(nodes=[node]))


def test_missing_fixed_id_is_rejected() -> None:
    assert check_workflow({"nodes": [], "connections": {}})


def test_the_run_workflows_name_the_error_workflow_that_closes_failed_runs() -> None:
    by_name = {path.name: json.loads(path.read_text()) for path in WORKFLOWS}
    error_workflow_id = by_name["05-run-error.json"]["id"]
    for name in ("01-receipts.json", "02-leads.json", "03-inbox.json"):
        assert by_name[name]["settings"]["errorWorkflow"] == error_workflow_id, name
    types = {node["type"] for node in by_name["05-run-error.json"]["nodes"]}
    assert "n8n-nodes-base.errorTrigger" in types
    assert by_name["05-run-error.json"]["active"] is False
