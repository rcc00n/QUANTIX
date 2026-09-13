"""Auditable research routes without fabricated CAD, fills or strategy versions."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005_arbitrage_engine"
down_revision = "0004_market_collectors"
branch_labels = None
depends_on = None


def record():
    return [sa.Column('id', sa.Uuid(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False)]


def upgrade():
    op.create_table('arbitrage_routes',
        *record(),
        sa.Column('route_key', sa.String(64), nullable=False),
        sa.Column('buy_market_id', sa.Uuid(), sa.ForeignKey('markets.id'), nullable=False),
        sa.Column('sell_market_id', sa.Uuid(), sa.ForeignKey('markets.id'), nullable=False),
        sa.Column('run_id', sa.Uuid(), sa.ForeignKey('collector_runs.id'), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('actionable', sa.Boolean(), nullable=False),
        sa.Column('detected_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('payload', postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint('id'), sa.UniqueConstraint('route_key'),
        sa.CheckConstraint('buy_market_id <> sell_market_id', name='distinct_markets'),
        sa.CheckConstraint('actionable = false', name='paper_research_only'),
        sa.CheckConstraint("status IN ('DETECTED','RECHECKING','VERIFIED_MARKET','RESEARCH_ONLY','INVALIDATED','EXPIRED')", name='lifecycle'))
    op.create_table('arbitrage_evaluations',
        *record(),
        sa.Column('route_id', sa.Uuid(), sa.ForeignKey('arbitrage_routes.id'), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('evaluated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('buy_snapshot_id', sa.Uuid(), sa.ForeignKey('order_book_snapshots.id'), nullable=False),
        sa.Column('sell_snapshot_id', sa.Uuid(), sa.ForeignKey('order_book_snapshots.id'), nullable=False),
        sa.Column('buy_fee_version_id', sa.Uuid(), sa.ForeignKey('fee_versions.id'), nullable=False),
        sa.Column('sell_fee_version_id', sa.Uuid(), sa.ForeignKey('fee_versions.id'), nullable=False),
        *[sa.Column(name, sa.Numeric(38,18), nullable=True) for name in
          ('quantity','acquisition_cost_cad','gross_profit_cad','net_profit_cad','net_return_pct')],
        sa.Column('calculation_version', sa.String(40), nullable=False),
        sa.Column('evidence', postgresql.JSONB(), nullable=False),
        sa.Column('result', postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint('id'), sa.UniqueConstraint('route_id','revision'))
    op.create_table('arbitrage_lifecycle_events',
        *record(),
        sa.Column('route_id', sa.Uuid(), sa.ForeignKey('arbitrage_routes.id'), nullable=False),
        sa.Column('event_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('details', postgresql.JSONB(), nullable=False), sa.PrimaryKeyConstraint('id'))
    for table in ('arbitrage_routes','arbitrage_evaluations','arbitrage_lifecycle_events'):
        op.create_index(f'ix_{table}_created_at', table, ['created_at'])
    op.create_index('ix_arbitrage_routes_detected_at','arbitrage_routes',['detected_at'])
    op.create_index('ix_arbitrage_routes_status','arbitrage_routes',['status'])
    for table in ('arbitrage_evaluations','arbitrage_lifecycle_events'):
        op.create_index(f'ix_{table}_route_id',table,['route_id'])
        op.execute(f'''CREATE TRIGGER immutable_evidence BEFORE UPDATE OR DELETE OR TRUNCATE
            ON {table} FOR EACH STATEMENT EXECUTE FUNCTION reject_evidence_mutation()''')


def downgrade():
    op.drop_table('arbitrage_lifecycle_events')
    op.drop_table('arbitrage_evaluations')
    op.drop_table('arbitrage_routes')
