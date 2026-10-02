"""Demo CRM accounts, loaded from samples/crm/accounts.csv."""

from __future__ import annotations

import csv
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from opskit.db.tables import crm_accounts

COLUMNS = ("name", "domain", "industry", "employee_band", "hq_city", "description")


def parse_accounts(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(newline="", encoding="utf-8") as handle:
        return [{column: row[column] for column in COLUMNS} for row in csv.DictReader(handle)]


async def upsert_accounts(session: AsyncSession, accounts: list[dict[str, str]]) -> int:
    if not accounts:
        return 0
    statement = insert(crm_accounts)
    statement = statement.on_conflict_do_update(
        index_elements=[crm_accounts.c.domain],
        set_={
            **{column: statement.excluded[column] for column in COLUMNS if column != "domain"},
            "updated_at": func.now(),
        },
    )
    await session.execute(statement, accounts)
    return len(accounts)
