"""leads: database bounds on every column the requester role can write.

Attaches `core.enforce_bounds()` (core_0010) to leads.research: text length, JSON type, size,
depth and item count, and number ranges. A lead with no city hint stores '' and one with no
website or domain stores NULL, so those stay allowed.

Rows already stored are never rewritten and never fail the upgrade: a bound is checked on insert
and on a changed column only, and the upgrade first reports the keys and the kind of problem of
stored rows outside the bounds (never the values: they may be client data).

Revision ID: leads_0003
Revises: leads_0002
"""

from alembic import op

from opskit.db import bounds as b

revision = "leads_0003"
down_revision = "leads_0002"
branch_labels = None
depends_on = ("core_0010",)  # core.enforce_bounds(), on another branch

HEX64 = r"^[0-9a-f]{64}$"

RESEARCH = {
    "company_name": b.text(200, min=1),
    "city_hint": b.text(100),
    "website": b.text(253, nullable=True),
    "domain": b.text(253, nullable=True),
    "reason": b.text(300, nullable=True),
    "fields": b.document("object", max_bytes=16384, depth=4),
    "findings": b.document("array", max_bytes=65536, depth=3, items=256),
    "pages": b.document("array", max_bytes=16384, depth=2, items=64),
    "raw_cites": b.number(0, 10000),
    "valid_cites": b.number(0, 10000),
    "replay_key": b.text(64, min=64, regex=HEX64, nullable=True),
    "cost_usd": b.number(0, 10000),
    "latency_ms": b.number(0, 100_000_000, nullable=True),
}

# (table, trigger, spec, key columns)
BOUNDS = [("leads.research", "research_bounds", RESEARCH, ["id"])]


def upgrade() -> None:
    for table, _, spec, key in BOUNDS:
        op.execute(b.report_sql(table, spec, key))
    for table, name, spec, _ in BOUNDS:
        op.execute(b.attach_sql(table, name, spec))


def downgrade() -> None:
    for table, name, _, _ in reversed(BOUNDS):
        op.execute(b.detach_sql(table, name))
