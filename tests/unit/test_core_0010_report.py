"""The upgrade shows a digest, not the file name, for the one core table keyed by client data."""

from __future__ import annotations

import re

import pytest

from opskit.db.migrations.versions import core_0010_column_bounds as migration


def test_only_sample_files_reports_hashed_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", statements.append)
    migration.upgrade()
    reports = [s for s in statements if "bounds_violations(" in s and "$report$" in s]
    assert len(reports) == len(migration.CORE_BOUNDS)
    for sql in reports:
        table = re.search(r"'(core\.\w+)'::regclass", sql)
        assert table is not None
        hashed = re.search(r", (true|false)\) v;", sql)
        assert hashed is not None
        assert (hashed.group(1) == "true") == (table.group(1) == "core.sample_files"), table.group(
            1
        )
