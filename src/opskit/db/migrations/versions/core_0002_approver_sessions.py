"""core: server-side approver sessions; let the app read the applied schema heads.

Revision ID: core_0002
Revises: core_0001
"""

from alembic import op

revision = "core_0002"
down_revision = "core_0001"
branch_labels = None
depends_on = None

APP_ROLE = "opskit_app"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE core.approver_sessions (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            csrf_token  text NOT NULL,
            created_at  timestamptz NOT NULL DEFAULT now(),
            expires_at  timestamptz NOT NULL,
            revoked_at  timestamptz
        );
        GRANT SELECT, INSERT, UPDATE ON core.approver_sessions TO {APP_ROLE};

        -- /healthz reports the applied migration heads.
        GRANT USAGE ON SCHEMA public TO {APP_ROLE};
        GRANT SELECT ON public.alembic_version TO {APP_ROLE};
        """
    )


def downgrade() -> None:
    op.execute("REVOKE SELECT ON public.alembic_version FROM opskit_app")
    op.execute("DROP TABLE core.approver_sessions")
