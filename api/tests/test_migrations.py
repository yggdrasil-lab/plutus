"""Regression tests for Alembic migrations and schema persistence."""

from pathlib import Path
import sqlite3

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig


def test_alembic_upgrade_head_creates_loans_and_commits(tmp_path: Path, monkeypatch) -> None:
    """Verify alembic upgrade head applies all migrations and tests 0017 <-> 0018 roundtrip."""
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

    assert "loans" in tables
    assert "transactions" in tables
    assert "categories" in tables
    assert "min_amount" in rule_columns
    assert "max_amount" in rule_columns
    assert "direction" in rule_columns
    assert "counterpart_account_id" in rule_columns
    assert "loan_id" in rule_columns
    assert "counterpart_account_id" in tx_columns
    assert "loan_id" in tx_columns
    assert "loan_payment_amount" in tx_columns
    assert "loan_type" in loan_columns
    assert version is not None
    assert version[0] == "0018_two_way_loans_system_cat"

    parent_by_name = {name: parent for name, parent, _ in seeded_categories}
    system_by_name = {name: bool(is_sys) for name, _, is_sys in seeded_categories}
    assert parent_by_name.get("Dining") == "Food"
    assert parent_by_name.get("Groceries") == "Food"
    assert parent_by_name.get("Alcohol") == "Food"
    assert parent_by_name.get("Fees & Admin") == "Finance"
    assert parent_by_name.get("Debt & Loans") == "Finance"
    assert system_by_name.get("Debt & Loans") is True

    # Verify 0018 -> 0017 downgrade -> upgrade roundtrip cleanly on SQLite
    alembic_command.downgrade(cfg, "0017_transaction_loan_link")
    with sqlite3.connect(db_file) as conn:
        down_ver = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        down_loan_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(loans)").fetchall()
        }
        down_tx_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(transactions)").fetchall()
        }
        debt_row = conn.execute(
            "SELECT is_system FROM categories WHERE LOWER(name) = 'debt & loans'"
        ).fetchone()
    assert down_ver is not None
    assert down_ver[0] == "0017_transaction_loan_link"
    assert "loan_type" not in down_loan_cols
    assert "loan_payment_amount" not in down_tx_cols
    assert debt_row is not None
    assert bool(debt_row[0]) is False

    alembic_command.upgrade(cfg, "head")
    with sqlite3.connect(db_file) as conn:
        reup_ver = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        reup_loan_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(loans)").fetchall()
        }
        reup_tx_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(transactions)").fetchall()
        }
        reup_debt_row = conn.execute(
            "SELECT is_system FROM categories WHERE LOWER(name) = 'debt & loans'"
        ).fetchone()
    assert reup_ver is not None
    assert reup_ver[0] == "0018_two_way_loans_system_cat"
    assert "loan_type" in reup_loan_cols
    assert "loan_payment_amount" in reup_tx_cols
    assert reup_debt_row is not None
    assert bool(reup_debt_row[0]) is True
