from datetime import date
import importlib.util
import json
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Account, BudgetRule, CategorizationRule, Transaction


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
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        acc = Account(
            name="ANZ",
            institution="ANZ",
            account_type="checking",
            tier="cash",
            currency="NZD",
        )
        session.add(acc)
        session.flush()

        session.add_all(
            [
                Transaction(
                    account_id=acc.id,
                    transaction_date=date(2026, 1, 15),
                    amount=-80.0,
                    currency="NZD",
                    merchant="Les Mills",
                    category="Gyms & Fitness",
                    raw_hash="h1",
                    tags="[]",
                ),
                Transaction(
                    account_id=acc.id,
                    transaction_date=date(2026, 1, 15),
                    amount=-6.5,
                    currency="NZD",
                    merchant="Mojo",
                    category="Cafes & Restaurants",
                    raw_hash="h2",
                    tags='["coffee"]',
                ),
                Transaction(
                    account_id=acc.id,
                    transaction_date=date(2026, 1, 15),
                    amount=-25.0,
                    currency="NZD",
                    merchant="Badminton",
                    category="Health",
                    raw_hash="h3",
                    tags='["badminton"]',
                ),
                CategorizationRule(
                    name="Gym Rule",
                    description_pattern="LES MILLS",
                    match_type="contains",
                    target_category="Gyms & Fitness",
                ),
                BudgetRule(
                    category="Gyms & Fitness",
                    weekly_limit=50.0,
                    monthly_limit=200.0,
                ),
            ]
        )
        session.commit()

    with engine.begin() as conn:
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
            sa.text("SELECT target_category FROM categorization_rules WHERE id = 1")
        ).scalar()
        assert rule_cat == "Health"

        budget_cat = conn.execute(
            sa.text("SELECT category FROM budget_rules WHERE id = 1")
        ).scalar()
        assert budget_cat == "Health"


def test_normalize_existing_legacy_categories_startup_cleanup():
    from app.services.connectors.akahu import normalize_existing_legacy_categories

    engine = sa.create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        acc = Account(
            name="ANZ",
            institution="ANZ",
            account_type="checking",
            tier="cash",
            currency="NZD",
        )
        session.add(acc)
        session.flush()

        samples = [
            ("CityFitness", "Gyms, fitness, aquatic facilities, yoga, pilates", "h1"),
            ("Chemist Warehouse", "Pharmacies", "h2"),
            ("McDonalds", "Fast food stores", "h3"),
            ("BP", "Fuel stations", "h4"),
            ("Woolworths", "Supermarkets and grocery stores", "h5"),
        ]
        for merchant, cat, rhash in samples:
            session.add(
                Transaction(
                    account_id=acc.id,
                    transaction_date=date(2026, 11, 10),
                    amount=-10.0,
                    currency="NZD",
                    merchant=merchant,
                    category=cat,
                    raw_hash=rhash,
                    tags="[]",
                )
            )
        session.commit()

        updated = normalize_existing_legacy_categories(session)
        assert updated == 5

        txs = session.query(Transaction).order_by(Transaction.id).all()
        assert [(t.category, json.loads(t.tags)) for t in txs] == [
            ("Health", ["gym-membership"]),
            ("Health", ["pharmacy"]),
            ("Food", ["fast-food"]),
            ("Transport", ["fuel"]),
            ("Groceries", ["supermarket"]),
        ]
        assert normalize_existing_legacy_categories(session) == 0
