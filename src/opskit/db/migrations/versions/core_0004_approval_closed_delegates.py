"""core: approvals gain closed_at and delegates (agent-core v0.1.0a3).

- closed_at is set exactly when the status is expired or cancelled; rows already expired
  get their expires_at.
- delegates holds the principals, besides the requester, that may consume the approval:
  a JSON array of at most 16 ids, fixed when the request is made.

Revision ID: core_0004
Revises: core_0003
"""

from alembic import op

revision = "core_0004"
down_revision = "core_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE core.approvals
            ADD COLUMN closed_at timestamptz,
            ADD COLUMN delegates jsonb NOT NULL DEFAULT '[]'::jsonb;
        UPDATE core.approvals SET closed_at = expires_at WHERE status = 'expired';
        ALTER TABLE core.approvals ADD CONSTRAINT approvals_closed_at_matches CHECK (
            (status IN ('expired', 'cancelled')) = (closed_at IS NOT NULL));
        ALTER TABLE core.approvals ADD CONSTRAINT approvals_delegates_shape CHECK (
            jsonb_typeof(delegates) = 'array' AND jsonb_array_length(delegates) <= 16);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE core.approvals DROP CONSTRAINT approvals_delegates_shape;
        ALTER TABLE core.approvals DROP CONSTRAINT approvals_closed_at_matches;
        ALTER TABLE core.approvals DROP COLUMN delegates, DROP COLUMN closed_at;
        """
    )
