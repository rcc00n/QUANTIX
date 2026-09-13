"""Preserve full exchange public-configuration history.

Revision ID: 0003_exchange_configs
Revises: 0002_audit_guards
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_exchange_configs"
down_revision = "0002_audit_guards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "exchange_config_versions",
        sa.Column("exchange_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("capabilities", postgresql.JSONB(), nullable=False),
        sa.Column("public_settings", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("version > 0", name=op.f("ck_exchange_config_versions_positive_version")),
        sa.ForeignKeyConstraint(["exchange_id"], ["exchanges.id"],
                                name=op.f("fk_exchange_config_versions_exchange_id_exchanges")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_exchange_config_versions")),
        sa.UniqueConstraint("exchange_id", "version", name=op.f("uq_exchange_config_versions_exchange_id")),
    )
    op.create_index("ix_exchange_config_versions_exchange_id", "exchange_config_versions", ["exchange_id"])
    op.create_index("ix_exchange_config_versions_created_at", "exchange_config_versions", ["created_at"])
    op.execute("""CREATE TRIGGER immutable_evidence
        BEFORE UPDATE OR DELETE OR TRUNCATE ON exchange_config_versions
        FOR EACH STATEMENT EXECUTE FUNCTION reject_evidence_mutation()""")


def downgrade() -> None:
    op.execute("DROP TRIGGER immutable_evidence ON exchange_config_versions")
    op.drop_table("exchange_config_versions")
