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
    from app.services.category_taxonomy import normalize_existing_legacy_categories

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
            ("Fitness", ["gym", "subscription"]),
            ("Medical", ["pharmacy"]),
            ("Dining", ["fast-food", "takeaway"]),
            ("Fuel", ["fuel"]),
            ("Groceries", ["supermarket"]),
        ]
        assert normalize_existing_legacy_categories(session) == 0


def test_normalize_existing_legacy_categories_six_tables_and_budget_collision():
    from app.models import Category, Product, Receipt, ReceiptItem, TransactionSplit
    from app.services.category_taxonomy import (
        CategoryCreateInput,
        create_category,
        ensure_default_categories,
        normalize_existing_legacy_categories,
    )

    engine = sa.create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        ensure_default_categories(session)
        create_category(session, CategoryCreateInput(name="Hobbies"))
        acc = Account(
            name="ANZ",
            institution="ANZ",
            account_type="checking",
            tier="cash",
            currency="NZD",
        )
        session.add(acc)
        session.flush()

        tx = Transaction(
            account_id=acc.id,
            transaction_date=date(2026, 11, 10),
            amount=-50.0,
            currency="NZD",
            merchant="Custom Hobby Shop",
            category="Hobbies",
            raw_hash="six-table-custom",
            tags="[]",
        )
        tx_legacy = Transaction(
            account_id=acc.id,
            transaction_date=date(2026, 11, 10),
            amount=-42.0,
            currency="NZD",
            merchant="CityFitness",
            category="Gyms & Fitness",
            raw_hash="six-table-legacy-tx",
            tags="[]",
        )
        split_parent = Transaction(
            account_id=acc.id,
            transaction_date=date(2026, 11, 10),
            amount=-30.0,
            currency="NZD",
            merchant="Woolworths",
            raw_description="WOOLWORTHS ALBANY",
            category="General",
            raw_hash="six-table-split-parent",
            tags="[]",
        )
        split_parent_blank = Transaction(
            account_id=acc.id,
            transaction_date=date(2026, 11, 10),
            amount=-25.0,
            currency="NZD",
            merchant="",
            raw_description=None,
            category="General",
            raw_hash="six-table-split-blank",
            tags="[]",
        )
        session.add_all([tx, tx_legacy, split_parent, split_parent_blank])
        session.flush()

        split = TransactionSplit(
            transaction_id=split_parent.id,
            amount=-30.0,
            category="Unknown",
        )
        split_no_hint = TransactionSplit(
            transaction_id=split_parent_blank.id,
            amount=-25.0,
            category="Fuel stations",
        )
        cat_rule = CategorizationRule(
            name="Chem Rule",
            description_pattern="Chemist Warehouse",
            match_type="contains",
            target_category="Pharmacies",
        )
        product = Product(name="Optimum Whey Protein", category="Legacy Supplements")
        receipt = Receipt(
            receipt_date=date(2026, 11, 10),
            merchant="Local Cafe",
            total_amount=8.5,
            currency="NZD",
            status="unlinked",
        )
        session.add_all([split, split_no_hint, cat_rule, product, receipt])
        session.flush()
        receipt_item = ReceiptItem(
            receipt_id=receipt.id,
            product_id=product.id,
            item_name="Flat White Coffee",
            unit_price=8.5,
            total_price=8.5,
            category="Cafes & Restaurants",
        )
        canonical_fitness_rule = BudgetRule(
            category="Fitness",
            weekly_limit=30.0,
            monthly_limit=130.0,
            is_active=False,
        )
        legacy_fitness_rule = BudgetRule(
            category="Gyms & Fitness",
            weekly_limit=45.0,
            monthly_limit=195.0,
            is_active=True,
        )
        inactive_dining_rule = BudgetRule(
            category="Cafes & Restaurants",
            weekly_limit=20.0,
            monthly_limit=86.67,
            is_active=False,
        )
        active_dining_rule = BudgetRule(
            category="Fast food stores",
            weekly_limit=35.0,
            monthly_limit=151.67,
            is_active=True,
        )
        session.add_all(
            [
                receipt_item,
                canonical_fitness_rule,
                legacy_fitness_rule,
                inactive_dining_rule,
                active_dining_rule,
            ]
        )
        session.commit()

        updated = normalize_existing_legacy_categories(session)
        assert updated == 9
        assert session.get(Transaction, tx.id).category == "Hobbies"
        assert session.get(Transaction, tx_legacy.id).category == "Fitness"
        assert json.loads(session.get(Transaction, tx_legacy.id).tags) == [
            "gym",
            "subscription",
        ]
        assert session.get(TransactionSplit, split.id).category == "Groceries"
        assert session.get(TransactionSplit, split_no_hint.id).category == "Fuel"
        assert session.get(CategorizationRule, cat_rule.id).target_category == "Medical"
        assert session.get(ReceiptItem, receipt_item.id).category == "Dining"
        assert session.get(Product, product.id).category == "Supplements"

        assert session.get(BudgetRule, legacy_fitness_rule.id) is None
        assert session.get(BudgetRule, canonical_fitness_rule.id).category == "Fitness"
        assert session.get(BudgetRule, inactive_dining_rule.id) is None
        kept_dining = session.get(BudgetRule, active_dining_rule.id)
        assert kept_dining is not None and kept_dining.category == "Dining"
        assert normalize_existing_legacy_categories(session) == 0
