"""Regression tests for Alembic migrations and schema persistence."""

from pathlib import Path
import sqlite3

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig


def test_alembic_upgrade_head_creates_loans_and_commits(tmp_path: Path, monkeypatch) -> None:
    """Verify alembic upgrade head applies and commits all migrations including 0008 loans."""
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

    assert "loans" in tables
    assert "transactions" in tables
    assert "categories" in tables
    assert "min_amount" in rule_columns
    assert "max_amount" in rule_columns
    assert "direction" in rule_columns
    assert "counterpart_account_id" in rule_columns
    assert "counterpart_account_id" in tx_columns
    assert version is not None
    assert version[0] == "0016_rule_counterpart_account"

    parent_by_name = {name: parent for name, parent, _ in seeded_categories}
    assert parent_by_name.get("Dining") == "Food"
    assert parent_by_name.get("Groceries") == "Food"
    assert parent_by_name.get("Alcohol") == "Food"
    assert parent_by_name.get("Fees & Admin") == "Finance"
    assert parent_by_name.get("Debt & Loans") == "Finance"

    # Verify 0016 downgrade -> upgrade roundtrip cleanly on SQLite
    alembic_command.downgrade(cfg, "0015_rule_amount_bounds")
    with sqlite3.connect(db_file) as conn:
        down_ver = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        down_rule_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(categorization_rules)").fetchall()
        }
        down_tx_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(transactions)").fetchall()
        }
    assert down_ver is not None
    assert down_ver[0] == "0015_rule_amount_bounds"
    assert "counterpart_account_id" not in down_rule_cols
    assert "counterpart_account_id" not in down_tx_cols

    alembic_command.upgrade(cfg, "head")
    with sqlite3.connect(db_file) as conn:
        reup_ver = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert reup_ver is not None
    assert reup_ver[0] == "0016_rule_counterpart_account"
