"""receipts: database bounds on every column the requester role can write.

Attaches `core.enforce_bounds()` (core_0010) to receipts.extractions and receipts.reconciliations:
text length and form, JSON type, size and depth, and number ranges. An unreadable file is stored
with an empty `sha256` and NULL `fields`, so both stay allowed.

Rows already stored are never rewritten and never fail the upgrade: a bound is checked on insert
and on a changed column only, and the upgrade first reports, per table, the keys and the kind of
problem of stored rows outside the bounds (never the values: they may be client data).

Revision ID: receipts_0003
Revises: receipts_0002
"""

from alembic import op

from opskit.db import bounds as b

revision = "receipts_0003"
down_revision = "receipts_0002"
branch_labels = None
depends_on = ("core_0010",)  # core.enforce_bounds(), on another branch

HEX64 = r"^[0-9a-f]{64}$"

EXTRACTIONS = {
    "path": b.text(512, min=1),
    "sha256": b.text(64, regex=r"^([0-9a-f]{64})?$"),
    "reason": b.text(200, nullable=True),
    "fields": b.document("object", max_bytes=65536, depth=4, nullable=True),
    "tier": b.text(100, nullable=True),
    "model": b.text(100, nullable=True),
    "replay_key": b.text(64, min=64, regex=HEX64, nullable=True),
    "cost_usd": b.number(0, 10000),
    "latency_ms": b.number(0, 100_000_000, nullable=True),
}
RECONCILIATIONS = {
    "rows": b.document("array", max_bytes=4_194_304, depth=4),
    "summary": b.document("object", max_bytes=4096, depth=2),
}

# (table, trigger, spec, key columns)
BOUNDS = [
    ("receipts.extractions", "extractions_bounds", EXTRACTIONS, ["path"]),
    ("receipts.reconciliations", "reconciliations_bounds", RECONCILIATIONS, ["id"]),
]


def upgrade() -> None:
    for table, _, spec, key in BOUNDS:
        op.execute(b.report_sql(table, spec, key))
    for table, name, spec, _ in BOUNDS:
        op.execute(b.attach_sql(table, name, spec))


def downgrade() -> None:
    for table, name, _, _ in reversed(BOUNDS):
        op.execute(b.detach_sql(table, name))
