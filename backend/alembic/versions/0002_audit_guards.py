"""Append-only evidence and deferred double-entry validation.

Revision ID: 0002_audit_guards
Revises: 0001_foundation
"""
from alembic import op

revision = "0002_audit_guards"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None

IMMUTABLE_TABLES = (
    "strategy_versions", "fee_versions", "fx_quotes", "market_observations",
    "order_book_snapshots", "opportunities", "signals", "journals", "ledger_entries",
    "virtual_accounts", "virtual_deposits", "balance_snapshots", "paper_fills",
    "simulation_events", "portfolio_valuations", "system_events", "actual_trades",
)


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION reject_evidence_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'append-only evidence: %', TG_TABLE_NAME USING ERRCODE = '23514';
        END $$;
    """)
    for table in IMMUTABLE_TABLES:
        op.execute(f"""CREATE TRIGGER immutable_evidence
            BEFORE UPDATE OR DELETE OR TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION reject_evidence_mutation()""")
    op.execute("""
        CREATE FUNCTION validate_journal_balance() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE journal_uuid uuid;
        BEGIN
            IF TG_TABLE_NAME = 'journals' THEN journal_uuid := NEW.id;
            ELSE journal_uuid := NEW.journal_id; END IF;
            IF (SELECT count(*) FROM ledger_entries WHERE journal_id = journal_uuid) < 2 THEN
                RAISE EXCEPTION 'journal requires at least two entries' USING ERRCODE = '23514';
            END IF;
            IF EXISTS (
                SELECT 1 FROM ledger_entries e
                JOIN virtual_accounts a ON a.id = e.account_id
                WHERE e.journal_id = journal_uuid
                GROUP BY a.asset_id HAVING sum(e.quantity) <> 0
            ) THEN
                RAISE EXCEPTION 'journal must balance per asset' USING ERRCODE = '23514';
            END IF;
            IF EXISTS (
                SELECT 1 FROM ledger_entries e
                JOIN virtual_accounts a ON a.id = e.account_id
                JOIN journals j ON j.id = e.journal_id
                WHERE j.id = journal_uuid AND a.session_id <> j.session_id
            ) THEN
                RAISE EXCEPTION 'cross-session journal prohibited' USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END $$;
    """)
    op.execute("""CREATE CONSTRAINT TRIGGER balanced_journal
        AFTER INSERT ON journals DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION validate_journal_balance()""")
    op.execute("""CREATE CONSTRAINT TRIGGER balanced_ledger
        AFTER INSERT ON ledger_entries DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION validate_journal_balance()""")
    # Block additions to an already committed journal. Entries must be inserted in
    # the same database transaction as the journal; retries use idempotency_key.
    op.execute("""
        CREATE FUNCTION reject_late_ledger_entry() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM journals
                WHERE id = NEW.journal_id AND xmin::text = pg_current_xact_id()::text) THEN
                RAISE EXCEPTION 'journal is already sealed' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute("""CREATE TRIGGER sealed_journal BEFORE INSERT ON ledger_entries
        FOR EACH ROW EXECUTE FUNCTION reject_late_ledger_entry()""")


def downgrade() -> None:
    op.execute("DROP TRIGGER sealed_journal ON ledger_entries")
    op.execute("DROP FUNCTION reject_late_ledger_entry()")
    op.execute("DROP TRIGGER balanced_ledger ON ledger_entries")
    op.execute("DROP TRIGGER balanced_journal ON journals")
    op.execute("DROP FUNCTION validate_journal_balance()")
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER immutable_evidence ON {table}")
    op.execute("DROP FUNCTION reject_evidence_mutation()")
