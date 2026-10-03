"""The SQL the bounds migrations generate must survive SQLAlchemy's text(), which reads
`:name` and `:300` as bind parameters (a bare JSON colon once broke the upgrade)."""

from __future__ import annotations

import importlib

import pytest
from sqlalchemy import text

from opskit.db import bounds as b

CORE = "opskit.db.migrations.versions.core_0010_column_bounds"
DOMAINS = [
    "opskit.db.migrations.versions.crm_0003_bounds",
    "opskit.db.migrations.versions.receipts_0003_bounds",
    "opskit.db.migrations.versions.leads_0003_bounds",
    "opskit.db.migrations.versions.inbox_0003_bounds",
]


def _statements() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    core = importlib.import_module(CORE)
    found.append(("core functions", core.FUNCTIONS))
    found.append(
        ("core cancel", core.CANCEL_LIVE_OUTSIDE_BOUNDS.format(spec=b.literal(core.APPROVALS)))
    )
    found.append(
        (
            "audit_log",
            b.attach_sql("core.audit_log", "audit_log_bounds", core.AUDIT_LOG, events="INSERT"),
        )
    )
    for table, name, spec, key in core.CORE_BOUNDS:
        found += [
            (f"{table} attach", b.attach_sql(table, name, spec)),
            (f"{table} report", b.report_sql(table, spec, key)),
        ]
    for module_name in DOMAINS:
        module = importlib.import_module(module_name)
        for table, name, spec, key in module.BOUNDS:
            found += [
                (f"{table} attach", b.attach_sql(table, name, spec)),
                (f"{table} report", b.report_sql(table, spec, key)),
            ]
    return found


@pytest.mark.parametrize(("label", "sql"), _statements(), ids=[label for label, _ in _statements()])
def test_generated_sql_has_no_bind_parameters(label: str, sql: str) -> None:
    assert text(sql).compile().params == {}, label


def test_the_literal_is_json_a_reader_can_parse_back() -> None:
    import json

    spec = {"a": b.text(5, regex=r"^[a-z:/-]+$"), "b": b.moment(max_s=300)}
    quoted = b.literal(spec)
    assert quoted.startswith("'") and quoted.endswith("'")
    assert json.loads(quoted[1:-1].replace("''", "'")) == spec
