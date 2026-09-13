"""Bounded pending/history queries and cross-record fill provenance guard."""
from alembic import op
revision="0008_paper_invariants"
down_revision="0007_paper_rebalancing"
branch_labels=None
depends_on=None


def upgrade():
    op.create_index('ix_paper_status_eligible','paper_trades',['status','eligible_at'])
    op.create_index('ix_paper_session_requested','paper_trades',['session_id','requested_at'])
    op.execute("""CREATE FUNCTION guard_paper_fill_provenance() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM paper_orders o JOIN paper_trades t ON t.id=o.trade_id
                JOIN journals j ON j.id=NEW.journal_id JOIN order_book_snapshots b ON b.id=NEW.snapshot_id
                JOIN fee_versions f ON f.id=NEW.fee_version_id JOIN markets m ON m.id=o.market_id
                WHERE o.id=NEW.order_id AND t.session_id=j.session_id AND b.market_id=o.market_id
                AND f.exchange_id=m.exchange_id AND (f.market_id IS NULL OR f.market_id=o.market_id)) THEN
                RAISE EXCEPTION 'paper fill provenance mismatch' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$""")
    op.execute("""CREATE TRIGGER paper_fill_provenance BEFORE INSERT ON paper_fills
        FOR EACH ROW EXECUTE FUNCTION guard_paper_fill_provenance()""")


def downgrade():
    op.execute('DROP TRIGGER paper_fill_provenance ON paper_fills')
    op.execute('DROP FUNCTION guard_paper_fill_provenance()')
    op.drop_index('ix_paper_session_requested',table_name='paper_trades')
    op.drop_index('ix_paper_status_eligible',table_name='paper_trades')
