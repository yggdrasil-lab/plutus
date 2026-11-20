"""Regression tests for Alembic migrations and schema persistence."""

from pathlib import Path
import sqlite3

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import (
    Account,
    CategorizationRule,
    Category,
    IngestedEmail,
    Loan,
    Receipt,
    Transaction,
    TransactionSplit,
)


def test_alembic_upgrade_head_creates_loans_and_commits(tmp_path: Path, monkeypatch) -> None:
    """Verify alembic upgrade head applies all migrations and tests 0019 <-> 0020 roundtrip."""
    db_file = tmp_path / "migrations_test.db"
    monkeypatch.setenv("PLUTUS_DATABASE_URL", f"sqlite:///{db_file}")

    api_root = Path(__file__).resolve().parent.parent
    cfg = AlembicConfig(str(api_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(api_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")

    alembic_command.upgrade(cfg, "head")

    with sqlite3.connect(db_file) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        seeded_categories = conn.execute(
            "SELECT c.name, p.name, c.is_system FROM categories c LEFT JOIN categories p ON p.id = c.parent_id"
        ).fetchall()
        rule_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(categorization_rules)").fetchall()
        }
        tx_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(transactions)").fetchall()
        }
        loan_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(loans)").fetchall()
        }
        split_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(transaction_splits)").fetchall()
        }
        account_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(accounts)").fetchall()
        }
        receipt_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(receipts)").fetchall()
        }
        ingested_email_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(ingested_emails)").fetchall()
        }

    assert "loans" in tables
    assert "transactions" in tables
    assert "transaction_splits" in tables
    assert "categories" in tables
    assert "ingested_emails" in tables
    assert "min_amount" in rule_columns
    assert "max_amount" in rule_columns
    assert "direction" in rule_columns
    assert "counterpart_account_id" in rule_columns
    assert "loan_id" in rule_columns
    assert "counterpart_account_id" in tx_columns
    assert "loan_id" in tx_columns
    assert "loan_payment_amount" in tx_columns
    assert "loan_type" in loan_columns
    assert "account_number_last4" in account_columns
    assert "order_number" in receipt_columns
    assert "source_email_message_id" in receipt_columns
    assert {
        "id",
        "message_id",
        "iris_email_id",
        "route_target",
        "status",
        "consumer_ref",
        "transaction_id",
        "receipt_ids_json",
        "error_detail",
        "created_at",
        "updated_at",
    }.issubset(ingested_email_columns)
    assert {
        "id",
        "transaction_id",
        "amount",
        "category",
        "tags",
        "loan_id",
        "notes",
        "created_at",
    }.issubset(split_columns)
    assert version is not None
    assert version[0] == "0020_email_ingestion"

    parent_by_name = {name: parent for name, parent, _ in seeded_categories}
    system_by_name = {name: bool(is_sys) for name, _, is_sys in seeded_categories}
    assert parent_by_name.get("Dining") == "Food"
    assert parent_by_name.get("Groceries") == "Food"
    assert parent_by_name.get("Alcohol") == "Food"
    assert parent_by_name.get("Fees & Admin") == "Finance"
    assert parent_by_name.get("Debt & Loans") == "Finance"
    assert system_by_name.get("Debt & Loans") is True

    # Verify 0020 -> 0019 downgrade -> seed DBS Live Fresh savings -> upgrade roundtrip cleanly on SQLite
    alembic_command.downgrade(cfg, "0019_transaction_splits")
    with sqlite3.connect(db_file) as conn:
        down_ver = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        down_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        down_account_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(accounts)").fetchall()
        }
        down_receipt_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(receipts)").fetchall()
        }
        conn.execute(
            "INSERT INTO accounts (name, account_type, tier, institution, currency, expected_annual_return, is_active, created_at, updated_at) "
            "VALUES ('DBS Live Fresh Visa', 'savings', 'cash', 'DBS Bank', 'SGD', 0.0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO accounts (name, account_type, tier, institution, currency, expected_annual_return, is_active, created_at, updated_at) "
            "VALUES ('ANZ Savings', 'savings', 'cash', 'ANZ', 'NZD', 0.0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        conn.commit()
    assert down_ver is not None
    assert down_ver[0] == "0019_transaction_splits"
    assert "ingested_emails" not in down_tables
    assert "account_number_last4" not in down_account_cols
    assert "order_number" not in down_receipt_cols
    assert "source_email_message_id" not in down_receipt_cols

    alembic_command.upgrade(cfg, "head")
    with sqlite3.connect(db_file) as conn:
        reup_ver = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        reup_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        reup_account_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(accounts)").fetchall()
        }
        reup_receipt_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(receipts)").fetchall()
        }
        acc_types = dict(
            conn.execute("SELECT name, account_type FROM accounts").fetchall()
        )
    assert reup_ver is not None
    assert reup_ver[0] == "0020_email_ingestion"
    assert "ingested_emails" in reup_tables
    assert "account_number_last4" in reup_account_cols
    assert {"order_number", "source_email_message_id"}.issubset(reup_receipt_cols)
    assert acc_types["DBS Live Fresh Visa"] == "credit_card"
    assert acc_types["ANZ Savings"] == "savings"

    # Verify ORM models select cleanly against the Alembic-migrated schema
    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with Session(engine) as session:
            for model in (
                Account,
                Category,
                Loan,
                Transaction,
                TransactionSplit,
                CategorizationRule,
                Receipt,
                IngestedEmail,
            ):
                session.scalars(select(model).limit(1)).all()
    finally:
        engine.dispose()


def test_all_alembic_revision_ids_fit_postgres_varchar32() -> None:
    """Guard against revision IDs exceeding PostgreSQL alembic_version.version_num VARCHAR(32)."""
    api_root = Path(__file__).resolve().parent.parent
    cfg = AlembicConfig(str(api_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(api_root / "alembic"))
    scripts = ScriptDirectory.from_config(cfg)
    too_long = [
        f"{rev.revision} ({len(rev.revision)} chars)"
        for rev in scripts.walk_revisions()
        if len(rev.revision) > 32
    ]
    assert too_long == []
