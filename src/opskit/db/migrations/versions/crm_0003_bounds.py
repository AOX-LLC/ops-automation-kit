"""crm: database bounds on every column the requester role can write.

Attaches `core.enforce_bounds()` (core_0010) to crm.accounts and crm.account_sources: text length,
the six research field names, and the founded-year range. The accounts the CSV seed and the leads
workflow write all fit; the caps are far wider than any sampled value.

Rows already stored are never rewritten and never fail the upgrade: a bound is checked on insert
and on a changed column only, and the upgrade first reports, per table, the keys and the kind of
problem of stored rows outside the bounds (never the values: they may be client data).

Revision ID: crm_0003
Revises: crm_0002
"""

from alembic import op

from opskit.db import bounds as b

revision = "crm_0003"
down_revision = "crm_0002"
branch_labels = None
depends_on = ("core_0010",)  # core.enforce_bounds(), on another branch

ACCOUNTS = {
    "name": b.text(200, min=1),
    "domain": b.text(253, nullable=True),
    "industry": b.text(200, nullable=True),
    "employee_band": b.text(32, nullable=True),
    "hq_city": b.text(200, nullable=True),
    "description": b.text(1000, nullable=True),
    "founded_year": b.number(1800, 2100, nullable=True),
}
ACCOUNT_SOURCES = {
    "field": b.enum(
        "domain", "industry", "employee_band", "hq_city", "founded_year", "description"
    ),
    "source_ref": b.text(512, min=1),
    "excerpt": b.text(1000, min=1),
}

# (table, trigger, spec, key columns)
BOUNDS = [
    ("crm.accounts", "accounts_bounds", ACCOUNTS, ["id"]),
    ("crm.account_sources", "account_sources_bounds", ACCOUNT_SOURCES, ["id"]),
]


def upgrade() -> None:
    for table, _, spec, key in BOUNDS:
        op.execute(b.report_sql(table, spec, key))
    for table, name, spec, _ in BOUNDS:
        op.execute(b.attach_sql(table, name, spec))


def downgrade() -> None:
    for table, name, _, _ in reversed(BOUNDS):
        op.execute(b.detach_sql(table, name))
