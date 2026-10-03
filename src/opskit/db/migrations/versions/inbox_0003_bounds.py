"""inbox: database bounds on every column the requester role can write.

Attaches `core.enforce_bounds()` (core_0010) to inbox.messages, inbox.triage and inbox.drafts: text
length, the triage category and priority, JSON type, size, depth and item count, and number and
time ranges. A failed draft stores an empty `to_addr`, `subject` and `body`, and a message may
have an empty subject or body, so those stay allowed. Mail is untrusted input: `inbox.store`
clamps what it stores from the mailbox to the MESSAGES limits before it inserts.

Rows already stored are never rewritten and never fail the upgrade: a bound is checked on insert
and on a changed column only, and the upgrade first reports, per table, the keys and the kind of
problem of stored rows outside the bounds (never the values: they may be client data).

Revision ID: inbox_0003
Revises: inbox_0002
"""

from alembic import op

from opskit.db import bounds as b

revision = "inbox_0003"
down_revision = "inbox_0002"
branch_labels = None
depends_on = ("core_0010",)  # core.enforce_bounds(), on another branch

HEX64 = r"^[0-9a-f]{64}$"
DAY = 86400

MESSAGES = {
    "message_id": b.text(998, min=1),
    "mailpit_id": b.text(200, min=1),
    "from_header": b.text(1000),
    "reply_to_header": b.text(2000, nullable=True),
    "to_addr": b.text(2000, nullable=True),
    "subject": b.text(2000),
    "received_at": b.moment(
        after="1990-01-01T00:00:00Z", before="2100-01-01T00:00:00Z", nullable=True
    ),
    "body_text": b.text(1_048_576),
}
TRIAGE = {
    "category": b.enum(
        "sales_inquiry",
        "support",
        "billing",
        "scheduling",
        "complaint",
        "vendor_invoice",
        "spam_phishing",
        "auto_reply",
        "newsletter",
        "other",
    ),
    "priority": b.enum("low", "normal", "high", "urgent"),
    "injection_reasons": b.document("array", max_bytes=16384, depth=3, items=16),
    "replay_key": b.text(64, min=64, regex=HEX64, nullable=True),
    "cost_usd": b.number(0, 10000),
    "latency_ms": b.number(0, 100_000_000, nullable=True),
}
DRAFTS = {
    "to_addr": b.text(320),
    "subject": b.text(998),
    "in_reply_to": b.text(998, nullable=True),
    "body": b.text(65536),
    "facts_used": b.document("array", max_bytes=16384, depth=2, items=64),
    "grounding": b.document("object", max_bytes=16384, depth=3),
    "failure_reason": b.text(200, nullable=True),
    "replay_key": b.text(64, min=64, regex=HEX64, nullable=True),
    "cost_usd": b.number(0, 10000),
    "latency_ms": b.number(0, 100_000_000, nullable=True),
    "sent_at": b.moment(min_s=-DAY, max_s=DAY, nullable=True),
}

# (table, trigger, spec, key columns)
BOUNDS = [
    ("inbox.messages", "messages_bounds", MESSAGES, ["message_id"]),
    ("inbox.triage", "triage_bounds", TRIAGE, ["message_id"]),
    ("inbox.drafts", "drafts_bounds", DRAFTS, ["id"]),
]


def upgrade() -> None:
    for table, _, spec, key in BOUNDS:
        op.execute(b.report_sql(table, spec, key))
    for table, name, spec, _ in BOUNDS:
        op.execute(b.attach_sql(table, name, spec))


def downgrade() -> None:
    for table, name, _, _ in reversed(BOUNDS):
        op.execute(b.detach_sql(table, name))
