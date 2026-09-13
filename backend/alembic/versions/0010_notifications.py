"""Durable owner-paired Telegram notifications and immutable alert audit."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0010_notifications"
down_revision = "0009_terminal_settings"
branch_labels = None
depends_on = None


def fk(name, target, nullable=False):
    return sa.Column(name, sa.Uuid(), sa.ForeignKey(target), nullable=nullable)


def timestamp(name, nullable=False):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def create(name, *columns):
    op.create_table(
        name,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        *columns,
    )
    op.create_index(f"ix_{name}_created_at", name, ["created_at"])


def upgrade():
    create(
        "telegram_challenges",
        fk("user_id", "users.id"),
        sa.Column("token_hash", sa.String(64), unique=True, nullable=False),
        timestamp("expires_at"),
        timestamp("requested_at", True),
        sa.Column("chat_id", sa.BigInteger()),
        sa.Column("telegram_user_id", sa.BigInteger()),
        sa.Column("telegram_username", sa.String(100)),
        timestamp("consumed_at", True),
    )
    op.create_index("ix_telegram_challenges_user_id", "telegram_challenges", ["user_id"])
    create(
        "telegram_bindings",
        fk("user_id", "users.id"),
        sa.Column("chat_id", sa.BigInteger(), unique=True, nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), unique=True, nullable=False),
        sa.Column("telegram_username", sa.String(100)),
        timestamp("approved_at"),
        sa.Column("paused", sa.Boolean(), nullable=False),
        timestamp("revoked_at", True),
        sa.UniqueConstraint("user_id"),
    )
    op.create_table(
        "notification_worker_state",
        sa.Column("key", sa.String(40), primary_key=True),
        sa.Column("update_offset", sa.BigInteger(), nullable=False),
        timestamp("heartbeat_at"),
        sa.Column("healthy", sa.Boolean(), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
    )
    create(
        "alert_decisions",
        fk("user_id", "users.id"),
        fk("evaluation_id", "arbitrage_evaluations.id"),
        fk("settings_version_id", "terminal_settings_versions.id", True),
        sa.Column("deduplication_key", sa.String(180), unique=True, nullable=False),
        timestamp("evaluated_at"),
        timestamp("expires_at"),
        sa.Column("classification", sa.String(24), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("emergency", sa.Boolean(), nullable=False),
        sa.Column("reasons", postgresql.JSONB(), nullable=False),
        sa.Column("rules", postgresql.JSONB(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_alert_decisions_user_id", "alert_decisions", ["user_id"])
    op.create_index("ix_alert_decisions_user_time", "alert_decisions", ["user_id", "evaluated_at"])
    create(
        "alert_lifecycle",
        fk("decision_id", "alert_decisions.id"),
        timestamp("acknowledged_at", True),
        fk("acknowledged_by", "users.id", True),
        timestamp("cancelled_at", True),
        sa.Column("repeat_count", sa.Integer(), nullable=False),
        timestamp("next_repeat_at", True),
        sa.UniqueConstraint("decision_id"),
    )
    create(
        "notification_outbox",
        fk("user_id", "users.id"),
        fk("binding_id", "telegram_bindings.id"),
        fk("alert_id", "alert_decisions.id", True),
        sa.Column("deduplication_key", sa.String(200), unique=True, nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("silent", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        timestamp("available_at"),
        timestamp("expires_at"),
        sa.Column("attempts", sa.Integer(), nullable=False),
        timestamp("delivered_at", True),
        sa.Column("provider_message_id", sa.String(100)),
        sa.Column("error_code", sa.String(80)),
    )
    for column in ("user_id", "alert_id", "status"):
        op.create_index(f"ix_notification_outbox_{column}", "notification_outbox", [column])
    op.create_index("ix_notification_outbox_due", "notification_outbox", ["status", "available_at"])
    create(
        "notification_events",
        fk("user_id", "users.id", True),
        fk("outbox_id", "notification_outbox.id", True),
        fk("alert_id", "alert_decisions.id", True),
        sa.Column("event_type", sa.String(60), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), unique=True),
    )
    for column in ("user_id", "event_type"):
        op.create_index(f"ix_notification_events_{column}", "notification_events", [column])
    for table in ("alert_decisions", "notification_events"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION reject_evidence_mutation()"
        )


def downgrade():
    for table in (
        "notification_events",
        "notification_outbox",
        "alert_lifecycle",
        "alert_decisions",
        "notification_worker_state",
        "telegram_bindings",
        "telegram_challenges",
    ):
        op.drop_table(table)
