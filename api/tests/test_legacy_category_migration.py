import importlib.util
import json
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa


def _load_migration_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0005_normalize_legacy_categories.py"
    )
    spec = importlib.util.spec_from_file_location("m0005", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_normalize_legacy_categories_migration():
    mod = _load_migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE transactions (id INTEGER PRIMARY KEY, category TEXT, tags TEXT)"
            )
        )
        conn.execute(
            sa.text("CREATE TABLE categorization_rules (id INTEGER PRIMARY KEY, category TEXT)")
        )
        conn.execute(
            sa.text("CREATE TABLE budget_rules (id INTEGER PRIMARY KEY, category TEXT UNIQUE)")
        )
        conn.execute(
            sa.text(
                "INSERT INTO transactions (id, category, tags) VALUES "
                "(1, 'Gyms & Fitness', '[]'), "
                "(2, 'Cafes & Restaurants', '[\"coffee\"]'), "
                "(3, 'Health', '[\"badminton\"]')"
            )
        )
        conn.execute(
            sa.text("INSERT INTO categorization_rules (id, category) VALUES (1, 'Gyms & Fitness')")
        )
        conn.execute(
            sa.text("INSERT INTO budget_rules (id, category) VALUES (1, 'Gyms & Fitness')")
        )

        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mod.op._proxy = Operations(ctx)
            mod.upgrade()

        txs = conn.execute(
            sa.text("SELECT id, category, tags FROM transactions ORDER BY id")
        ).fetchall()
        assert txs[0][1] == "Health"
        assert json.loads(txs[0][2]) == ["gyms-fitness"]
        assert txs[1][1] == "Food"
        assert json.loads(txs[1][2]) == ["coffee", "cafes-restaurants"]
        assert txs[2][1] == "Health"
        assert json.loads(txs[2][2]) == ["badminton"]

        rule_cat = conn.execute(
            sa.text("SELECT category FROM categorization_rules WHERE id = 1")
        ).scalar()
        assert rule_cat == "Health"

        budget_cat = conn.execute(
            sa.text("SELECT category FROM budget_rules WHERE id = 1")
        ).scalar()
        assert budget_cat == "Health"
