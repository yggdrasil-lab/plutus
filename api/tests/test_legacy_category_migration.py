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


def test_normalize_preserves_adhoc_budget_rules_and_deactivated_categories():
    from app.models import Category, Product, Receipt, ReceiptItem, TransactionSplit
    from app.services.category_taxonomy import (
        CategoryCreateInput,
        CategoryUpdateInput,
        create_category,
        ensure_default_categories,
        normalize_existing_legacy_categories,
        update_category,
    )

    engine = sa.create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        ensure_default_categories(session)
        hobbies = create_category(session, CategoryCreateInput(name="Hobbies"))
        food = session.scalar(sa.select(Category).where(Category.name == "Food"))
        assert food is not None
        for child in session.scalars(sa.select(Category).where(Category.parent_id == food.id)).all():
            update_category(session, child.id, CategoryUpdateInput(is_active=False))
        update_category(session, food.id, CategoryUpdateInput(is_active=False))
        update_category(session, hobbies.id, CategoryUpdateInput(is_active=False))

        acc = Account(
            name="ANZ",
            institution="ANZ",
            account_type="checking",
            tier="cash",
            currency="NZD",
        )
        session.add(acc)
        session.flush()

        tx_food = Transaction(
            account_id=acc.id,
            transaction_date=date(2026, 11, 10),
            amount=-20.0,
            currency="NZD",
            merchant="Corner Eatery",
            category="Food",
            raw_hash="deact-food-tx",
            tags="[]",
        )
        tx_hobbies = Transaction(
            account_id=acc.id,
            transaction_date=date(2026, 11, 10),
            amount=-45.0,
            currency="NZD",
            merchant="Hobby Co",
            category="Hobbies",
            raw_hash="deact-hobbies-tx",
            tags="[]",
        )
        session.add_all([tx_food, tx_hobbies])
        session.flush()

        split_food = TransactionSplit(
            transaction_id=tx_food.id, amount=-10.0, category="Food"
        )
        split_hobbies = TransactionSplit(
            transaction_id=tx_hobbies.id, amount=-15.0, category="Hobbies"
        )
        cat_rule_food = CategorizationRule(
            name="Food Rule",
            description_pattern="Eatery",
            match_type="contains",
            target_category="Food",
        )
        cat_rule_hobbies = CategorizationRule(
            name="Hobby Rule",
            description_pattern="Hobby Co",
            match_type="contains",
            target_category="Hobbies",
        )
        prod_food = Product(name="Snack Pack", category="Food")
        prod_hobbies = Product(name="Paint Set", category="Hobbies")
        receipt = Receipt(
            receipt_date=date(2026, 11, 10),
            merchant="Corner Eatery",
            total_amount=20.0,
            currency="NZD",
            status="unlinked",
        )
        session.add_all(
            [
                split_food,
                split_hobbies,
                cat_rule_food,
                cat_rule_hobbies,
                prod_food,
                prod_hobbies,
                receipt,
            ]
        )
        session.flush()

        item_food = ReceiptItem(
            receipt_id=receipt.id,
            product_id=prod_food.id,
            item_name="Sandwich",
            unit_price=10.0,
            total_price=10.0,
            category="Food",
        )
        item_hobbies = ReceiptItem(
            receipt_id=receipt.id,
            product_id=prod_hobbies.id,
            item_name="Brush",
            unit_price=10.0,
            total_price=10.0,
            category="Hobbies",
        )
        general_rule = BudgetRule(
            category="General", weekly_limit=40.0, monthly_limit=173.33, is_active=True
        )
        adhoc_rule = BudgetRule(
            category="Education", weekly_limit=30.0, monthly_limit=130.0, is_active=True
        )
        food_rule = BudgetRule(
            category="Food", weekly_limit=60.0, monthly_limit=260.0, is_active=False
        )
        hobbies_rule = BudgetRule(
            category="Hobbies", weekly_limit=25.0, monthly_limit=108.33, is_active=False
        )
        session.add_all(
            [item_food, item_hobbies, general_rule, adhoc_rule, food_rule, hobbies_rule]
        )
        session.commit()

        assert normalize_existing_legacy_categories(session) == 0
        assert session.get(Transaction, tx_food.id).category == "Food"
        assert session.get(Transaction, tx_hobbies.id).category == "Hobbies"
        assert session.get(TransactionSplit, split_food.id).category == "Food"
        assert session.get(TransactionSplit, split_hobbies.id).category == "Hobbies"
        assert session.get(CategorizationRule, cat_rule_food.id).target_category == "Food"
        assert session.get(CategorizationRule, cat_rule_hobbies.id).target_category == "Hobbies"
        assert session.get(ReceiptItem, item_food.id).category == "Food"
        assert session.get(ReceiptItem, item_hobbies.id).category == "Hobbies"
        assert session.get(Product, prod_food.id).category == "Food"
        assert session.get(Product, prod_hobbies.id).category == "Hobbies"
        assert session.get(BudgetRule, adhoc_rule.id).category == "Education"
        assert session.get(BudgetRule, general_rule.id).category == "General"
        assert session.get(BudgetRule, food_rule.id).category == "Food"
        assert session.get(BudgetRule, hobbies_rule.id).category == "Hobbies"
