"""Versioned terminal settings and idempotent independent research experiments."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0009_terminal_settings"
down_revision = "0008_paper_invariants"
branch_labels = None
depends_on = None


def upgrade():
    for name, columns, constraints in (
        (
            "terminal_settings_versions",
            [
                sa.Column("version", sa.Integer(), nullable=False),
                sa.Column("settings", postgresql.JSONB(), nullable=False),
                sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
            ],
            [sa.UniqueConstraint("user_id", "version")],
        ),
        (
            "funding_proposals",
            [
                sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
                sa.Column("payload", postgresql.JSONB(), nullable=False),
            ],
            [],
        ),
        (
            "funding_experiments",
            [
                sa.Column(
                    "session_id", sa.Uuid(), sa.ForeignKey("paper_sessions.id"), nullable=False
                ),
                sa.Column("idempotency_key", sa.String(100), nullable=False),
                sa.Column("fingerprint", sa.String(64), nullable=False),
                sa.Column("source_session_id", sa.Uuid(), sa.ForeignKey("paper_sessions.id")),
            ],
            [sa.UniqueConstraint("user_id", "idempotency_key")],
        ),
    ):
        op.create_table(
            name,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
            *columns,
            *constraints,
        )
        op.create_index(f"ix_{name}_created_at", name, ["created_at"])
        op.create_index(f"ix_{name}_user_id", name, ["user_id"])
        op.execute(
            f"CREATE TRIGGER {name}_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON {name} "
            "FOR EACH STATEMENT EXECUTE FUNCTION reject_evidence_mutation()"
        )


def downgrade():
    for name in ("funding_experiments", "funding_proposals", "terminal_settings_versions"):
        op.drop_table(name)
