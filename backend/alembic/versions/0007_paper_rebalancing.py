"""Causal delayed rebalancing requests, separate from trading orders."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision="0007_paper_rebalancing"
down_revision="0006_paper_accounting"
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('paper_rebalance_requests',
        sa.Column('id',sa.Uuid(),primary_key=True),
        sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.text('now()'),nullable=False),
        sa.Column('session_id',sa.Uuid(),sa.ForeignKey('paper_sessions.id'),nullable=False),
        sa.Column('from_exchange_id',sa.Uuid(),sa.ForeignKey('exchanges.id'),nullable=False),
        sa.Column('to_exchange_id',sa.Uuid(),sa.ForeignKey('exchanges.id'),nullable=False),
        sa.Column('asset_id',sa.Uuid(),sa.ForeignKey('assets.id'),nullable=False),
        sa.Column('requested_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('eligible_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('status',sa.String(16),nullable=False),
        sa.Column('request',postgresql.JSONB(),nullable=False),
        sa.Column('result',postgresql.JSONB(),nullable=False))
    op.create_index('ix_paper_rebalance_requests_session_id','paper_rebalance_requests',['session_id'])
    op.create_index('ix_paper_rebalance_requests_created_at','paper_rebalance_requests',['created_at'])


def downgrade():
    op.drop_table('paper_rebalance_requests')
