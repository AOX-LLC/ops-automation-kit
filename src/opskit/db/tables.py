"""Table definitions mirroring the migrations. Only `opskit.core` touches the `core` tables."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    func,
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
    Column("reason", Text),
    Column("run_context", JSONB),
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
