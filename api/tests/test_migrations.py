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

    assert "loans" in tables
    assert "transactions" in tables
    assert "categories" in tables
    assert version is not None
    assert version[0] == "0014_categories_table"

    parent_by_name = {name: parent for name, parent, _ in seeded_categories}
    assert parent_by_name.get("Dining") == "Food"
    assert parent_by_name.get("Groceries") == "Food"
    assert parent_by_name.get("Alcohol") == "Food"
    assert parent_by_name.get("Fees & Admin") == "Finance"
    assert parent_by_name.get("Debt & Loans") == "Finance"
