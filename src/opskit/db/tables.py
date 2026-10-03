"""Table definitions mirroring the migrations. Only `opskit.core` touches the `core` tables."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData()


def _created_at(name: str = "created_at") -> Column[datetime]:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


runs = Table(
    "runs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("workflow", Text, nullable=False),
    Column("n8n_workflow_id", Text),
    Column("n8n_execution_id", Text, unique=True),
    Column("mode", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="running"),
    _created_at("started_at"),
    Column("finished_at", DateTime(timezone=True)),
    schema="core",
)

approvals = Table(
    "approvals",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("run_id", UUID(as_uuid=True), ForeignKey("core.runs.id")),
    Column("action", Text, nullable=False),
    Column("summary", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("payload_sha256", String(64), nullable=False),
    Column("requested_by", Text, nullable=False),
    Column("required_role", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="pending"),
    Column("decision", Text),
    Column("resume_url", Text),
    _created_at(),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("resolved_at", DateTime(timezone=True)),
    Column("resolved_by", Text),
    Column("consumed_at", DateTime(timezone=True)),
    Column("closed_at", DateTime(timezone=True)),
    Column("reason", Text),
    Column("run_context", JSONB(none_as_null=True)),
    Column("delegates", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    schema="core",
)

outbox = Table(
    "outbox",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("approval_id", UUID(as_uuid=True), ForeignKey("core.approvals.id"), unique=True),
    Column("payload", JSONB, nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("delivered_at", DateTime(timezone=True)),
    Column("last_error", Text),
    schema="core",
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("seq", BigInteger, primary_key=True, autoincrement=False),
    Column("schema_version", Integer, nullable=False),
    Column("event_id", UUID(as_uuid=True), nullable=False, unique=True),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("action", Text, nullable=False),
    Column("actor_id", Text, nullable=False),
    Column("subject_id", Text),
    Column("payload", Text, nullable=False),  # canonical JSON text, exactly as hashed
    Column("run_context", Text),
    Column("prev_hash", String(64), nullable=False),
    Column("record_hash", String(64), nullable=False, unique=True),
    Column("db_role", Text),  # set by an insert trigger, whatever was sent; not in the hash
    schema="core",
)

model_calls = Table(
    "model_calls",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("run_id", UUID(as_uuid=True), ForeignKey("core.runs.id")),
    Column("prompt_id", Text, nullable=False),
    Column("prompt_version", Integer, nullable=False),
    Column("tier", Text, nullable=False),
    Column("replay_key", String(64), nullable=False),
    Column("model", Text),
    Column("mode", Text, nullable=False),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("cost_usd", Numeric(10, 6), nullable=False),
    Column("latency_ms", Integer, nullable=False),
    _created_at(),
    schema="core",
)

approver_sessions = Table(
    "approver_sessions",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("csrf_token", Text, nullable=False),
    _created_at(),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True)),
    schema="core",
)

sample_files = Table(
    "sample_files",
    metadata,
    Column("path", Text, primary_key=True),
    Column("workflow", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("bytes", Integer, nullable=False),
    _created_at("loaded_at"),
    schema="core",
)

crm_accounts = Table(
    "accounts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("name", Text, nullable=False),
    Column("domain", Text, unique=True),
    Column("industry", Text),
    Column("employee_band", Text),
    Column("hq_city", Text),
    Column("description", Text),
    Column("founded_year", Integer),
    _created_at(),
    _created_at("updated_at"),
    schema="crm",
)

crm_account_sources = Table(
    "account_sources",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("account_id", UUID(as_uuid=True), ForeignKey("crm.accounts.id"), nullable=False),
    Column("field", Text, nullable=False),
    Column("source_ref", Text, nullable=False),
    Column("excerpt", Text, nullable=False),
    _created_at("found_at"),
    UniqueConstraint("account_id", "field", name="account_sources_account_field_key"),
    schema="crm",
)

receipt_extractions = Table(
    "extractions",
    metadata,
    Column("path", Text, primary_key=True),
    Column("sha256", Text, nullable=False),
    Column("run_id", UUID(as_uuid=True), ForeignKey("core.runs.id"), nullable=False),
    Column("status", Text, nullable=False),
    Column("reason", Text),
    Column("fields", JSONB),
    Column("replay_key", String(64)),
    Column("tier", Text),
    Column("model", Text),
    Column("cost_usd", Numeric(10, 6), nullable=False, server_default="0"),
    Column("latency_ms", Integer),
    _created_at("extracted_at"),
    schema="receipts",
)

receipt_reconciliations = Table(
    "reconciliations",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("run_id", UUID(as_uuid=True), ForeignKey("core.runs.id"), nullable=False),
    Column("rows", JSONB, nullable=False),
    Column("summary", JSONB, nullable=False),
    _created_at(),
    schema="receipts",
)

inbox_messages = Table(
    "messages",
    metadata,
    Column("message_id", Text, primary_key=True),
    Column("mailpit_id", Text, nullable=False),
    Column("from_header", Text, nullable=False),
    Column("reply_to_header", Text),
    Column("to_addr", Text),
    Column("subject", Text, nullable=False),
    Column("received_at", DateTime(timezone=True)),
    Column("body_text", Text, nullable=False),
    _created_at("fetched_at"),
    schema="inbox",
)

inbox_triage = Table(
    "triage",
    metadata,
    Column("message_id", Text, ForeignKey("inbox.messages.message_id"), primary_key=True),
    Column("run_id", UUID(as_uuid=True), ForeignKey("core.runs.id"), nullable=False),
    Column("category", Text, nullable=False),
    Column("priority", Text, nullable=False),
    Column("needs_reply", Boolean, nullable=False),
    Column("escalate", Boolean, nullable=False),
    Column("route", Text, nullable=False),
    Column("quarantined", Boolean, nullable=False),
    Column("injection_reasons", JSONB, nullable=False, server_default="[]"),
    Column("replay_key", String(64)),
    Column("cost_usd", Numeric(10, 6), nullable=False, server_default="0"),
    Column("latency_ms", Integer),
    _created_at("triaged_at"),
    schema="inbox",
)

inbox_drafts = Table(
    "drafts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "message_id", Text, ForeignKey("inbox.messages.message_id"), nullable=False, unique=True
    ),
    Column("run_id", UUID(as_uuid=True), ForeignKey("core.runs.id"), nullable=False),
    Column("approval_id", UUID(as_uuid=True), ForeignKey("core.approvals.id")),
    Column("to_addr", Text, nullable=False),
    Column("subject", Text, nullable=False),
    Column("in_reply_to", Text),
    Column("body", Text, nullable=False),
    Column("facts_used", JSONB, nullable=False, server_default="[]"),
    Column("grounding", JSONB, nullable=False, server_default="{}"),
    Column("reply_to_differs", Boolean, nullable=False, server_default="false"),
    Column("status", Text, nullable=False),
    Column("failure_reason", Text),
    Column("replay_key", String(64)),
    Column("cost_usd", Numeric(10, 6), nullable=False, server_default="0"),
    Column("latency_ms", Integer),
    _created_at(),
    Column("sent_at", DateTime(timezone=True)),
    schema="inbox",
)

leads_research = Table(
    "research",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("run_id", UUID(as_uuid=True), ForeignKey("core.runs.id"), nullable=False),
    Column("company_name", Text, nullable=False),
    Column("city_hint", Text, nullable=False),
    Column("website", Text),
    Column("domain", Text),
    Column("status", Text, nullable=False),
    Column("reason", Text),
    Column("fields", JSONB, nullable=False),
    Column("findings", JSONB, nullable=False, server_default="[]"),
    Column("pages", JSONB, nullable=False, server_default="[]"),
    Column("raw_cites", Integer, nullable=False, server_default="0"),
    Column("valid_cites", Integer, nullable=False, server_default="0"),
    Column("replay_key", String(64)),
    Column("cost_usd", Numeric(10, 6), nullable=False, server_default="0"),
    Column("latency_ms", Integer),
    Column("crm_action", Text),
    Column("account_id", UUID(as_uuid=True), ForeignKey("crm.accounts.id")),
    _created_at(),
    UniqueConstraint("run_id", "company_name", "city_hint"),
    schema="leads",
)
