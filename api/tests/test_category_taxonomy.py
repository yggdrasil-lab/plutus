"""SQLite :memory: interface tests for the deep Category Taxonomy package."""

from collections.abc import Iterator
from dataclasses import FrozenInstanceError, is_dataclass
from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import (
    Account,
    BudgetRule,
    CategorizationRule,
    Category,
    Product,
    Receipt,
    ReceiptItem,
    Transaction,
    TransactionSplit,
)
from app.services.category_taxonomy import (
    AnnualCategoryTaxonomy,
    CategoryConflictError,
    CategoryCreateInput,
    CategoryDeleteSummary,
    CategoryMetrics,
    CategoryNotFoundError,
    CategorySnapshot,
    CategoryUpdateInput,
    CategoryValidationError,
    create_category,
    delete_category,
    ensure_default_categories,
    get_active_categories,
    get_annual_category_taxonomy,
    get_canonical_category_names,
    get_category_parent_map,
    get_category_snapshot,
    get_descendant_category_names,
    get_parent_category,
    get_root_category,
    list_categories,
    normalize_existing_legacy_categories,
    resolve_budget_rule_category,
    sync_budget_rule_amortization,
    update_category,
)


def _new_memory_session_factory() -> tuple[sessionmaker[Session], object]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine), engine


@pytest.fixture()
def db_session() -> Iterator[Session]:
    """Provide an isolated SQLite :memory: session with default taxonomy seeded."""
    session_factory, engine = _new_memory_session_factory()
    db = session_factory()
    ensure_default_categories(db)
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def seeded_account(db_session: Session) -> Account:
    """Create a synthetic checking account for transaction fixtures."""
    account = Account(
        name="Everyday Checking",
        account_type="checking",
        tier="cash",
        institution="Kiwibank",
        currency="NZD",
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


def _create_six_table_fixtures(
    db: Session, account_id: int, category_name: str
) -> dict[str, int]:
    today = date.today()
    tx = Transaction(
        transaction_date=today,
        account_id=account_id,
        amount=-120.0,
        currency="NZD",
        merchant="Oak Market",
        category=category_name,
        raw_hash=f"tx-{category_name.lower()}-{account_id}",
    )
    db.add(tx)
    db.flush()
    split_tx = Transaction(
        transaction_date=today,
        account_id=account_id,
        amount=-60.0,
        currency="NZD",
        merchant="Corner Store",
        category="General",
        raw_hash=f"split-parent-{category_name.lower()}-{account_id}",
    )
    db.add(split_tx)
    db.flush()
    split = TransactionSplit(transaction_id=split_tx.id, amount=-35.0, category=category_name)
    rule = CategorizationRule(
        name=f"{category_name} Rule",
        description_pattern="Oak Market",
        match_type="contains",
        target_category=category_name,
    )
    budget = BudgetRule(
        category=category_name,
        weekly_limit=40.0,
        monthly_limit=173.33,
        budget_period="monthly",
        is_active=True,
    )
    product = Product(name=f"{category_name} Item", category=category_name)
    receipt = Receipt(
        receipt_date=today,
        merchant="Oak Market",
        total_amount=50.0,
        currency="NZD",
        status="matched",
        transaction_id=tx.id,
    )
    db.add_all([split, rule, budget, product, receipt])
    db.flush()
    item = ReceiptItem(
        receipt_id=receipt.id,
        product_id=product.id,
        item_name=f"{category_name} Line",
        unit_price=50.0,
        total_price=50.0,
        category=category_name,
    )
    db.add(item)
    db.commit()
    return {
        "tx_id": tx.id,
        "split_id": split.id,
        "rule_id": rule.id,
        "budget_id": budget.id,
        "product_id": product.id,
        "item_id": item.id,
    }


def test_detached_frozen_contracts_survive_session_close() -> None:
    session_factory, engine = _new_memory_session_factory()
    try:
        with session_factory() as db:
            created = create_category(db, CategoryCreateInput(name="Hobbies"))
            listed = list_categories(db)
            summary = delete_category(db, created.id)

        assert is_dataclass(created) and isinstance(created, CategorySnapshot)
        assert isinstance(created.metrics, CategoryMetrics)
        assert is_dataclass(summary) and isinstance(summary, CategoryDeleteSummary)
        assert len(listed) >= 33
        assert summary.deleted_name == "Hobbies"
        assert summary.reassigned_to == "General"
        with pytest.raises(FrozenInstanceError):
            created.name = "Mutated"  # type: ignore[misc]
        with pytest.raises(FrozenInstanceError):
            summary.reassigned_to = "Food"  # type: ignore[misc]
    finally:
        Base.metadata.drop_all(bind=engine)


def test_rename_cascades_across_all_six_tables_including_splits(
    db_session: Session, seeded_account: Account
) -> None:
    food = next(c for c in list_categories(db_session) if c.name == "Food")
    created = create_category(
        db_session, CategoryCreateInput(name="Coffee Beans", parent_id=food.id)
    )
    ids = _create_six_table_fixtures(db_session, seeded_account.id, "Coffee Beans")

    updated = update_category(
        db_session, created.id, CategoryUpdateInput(name="Specialty Roast")
    )

    assert updated.name == "Specialty Roast"
    assert db_session.get(Transaction, ids["tx_id"]).category == "Specialty Roast"
    assert db_session.get(TransactionSplit, ids["split_id"]).category == "Specialty Roast"
    assert db_session.get(CategorizationRule, ids["rule_id"]).target_category == "Specialty Roast"
    assert db_session.get(Product, ids["product_id"]).category == "Specialty Roast"
    assert db_session.get(ReceiptItem, ids["item_id"]).category == "Specialty Roast"
    assert db_session.get(BudgetRule, ids["budget_id"]).category == "Specialty Roast"
    orphaned_splits = db_session.scalars(
        select(TransactionSplit).where(TransactionSplit.category == "Coffee Beans")
    ).all()
    assert orphaned_splits == []


def test_case_only_rename_updates_budget_rule_casing(
    db_session: Session, seeded_account: Account
) -> None:
    created = create_category(db_session, CategoryCreateInput(name="custom dining"))
    ids = _create_six_table_fixtures(db_session, seeded_account.id, "custom dining")

    updated = update_category(
        db_session, created.id, CategoryUpdateInput(name="Custom Dining")
    )

    assert updated.name == "Custom Dining"
    assert db_session.get(BudgetRule, ids["budget_id"]).category == "Custom Dining"
    assert db_session.get(TransactionSplit, ids["split_id"]).category == "Custom Dining"


def test_rename_collision_keeps_target_budget_rule_and_removes_source(
    db_session: Session,
) -> None:
    old_cat = create_category(db_session, CategoryCreateInput(name="Old Travel"))
    source_rule = BudgetRule(
        category="Old Travel",
        weekly_limit=30.0,
        monthly_limit=130.0,
        budget_period="monthly",
        is_active=True,
    )
    orphan_target_rule = BudgetRule(
        category="holiday",
        weekly_limit=75.0,
        monthly_limit=325.0,
        budget_period="annual",
        is_active=True,
    )
    db_session.add_all([source_rule, orphan_target_rule])
    db_session.commit()

    renamed = update_category(
        db_session, old_cat.id, CategoryUpdateInput(name="Holiday")
    )

    assert renamed.name == "Holiday"
    assert renamed.has_budget_rule is True
    assert renamed.weekly_limit == 75.0
    assert renamed.budget_period == "annual"
    assert db_session.get(BudgetRule, source_rule.id) is None
    kept = db_session.get(BudgetRule, orphan_target_rule.id)
    assert kept is not None and kept.category == "Holiday"


def test_delete_category_cascades_all_six_tables_and_counts_splits(
    db_session: Session, seeded_account: Account
) -> None:
    food = next(c for c in list_categories(db_session) if c.name == "Food")
    created = create_category(
        db_session, CategoryCreateInput(name="Bakery", parent_id=food.id)
    )
    ids = _create_six_table_fixtures(db_session, seeded_account.id, "Bakery")

    summary = delete_category(db_session, created.id)

    assert summary.deleted_name == "Bakery"
    assert summary.reassigned_to == "Food"
    assert summary.transactions_reassigned == 1
    assert summary.splits_reassigned == 1
    assert summary.rules_reassigned == 1
    assert summary.receipt_items_reassigned == 1
    assert summary.products_reassigned == 1
    assert summary.children_promoted == 0
    assert db_session.get(Transaction, ids["tx_id"]).category == "Food"
    assert db_session.get(TransactionSplit, ids["split_id"]).category == "Food"
    assert db_session.get(CategorizationRule, ids["rule_id"]).target_category == "Food"
    assert db_session.get(ReceiptItem, ids["item_id"]).category == "Food"
    assert db_session.get(Product, ids["product_id"]).category == "Food"
    assert db_session.get(BudgetRule, ids["budget_id"]) is None


def test_metrics_distinct_transaction_count_includes_splits_without_double_counting(
    db_session: Session, seeded_account: Account
) -> None:
    today = date.today()
    tx_both = Transaction(
        transaction_date=today,
        account_id=seeded_account.id,
        amount=-100.0,
        currency="NZD",
        merchant="Fresh Mart",
        category="Groceries",
        raw_hash="metrics-both",
    )
    tx_split_only = Transaction(
        transaction_date=today,
        account_id=seeded_account.id,
        amount=-80.0,
        currency="NZD",
        merchant="Corner Mart",
        category="General",
        raw_hash="metrics-split-only",
    )
    db_session.add_all([tx_both, tx_split_only])
    db_session.flush()
    db_session.add_all(
        [
            TransactionSplit(transaction_id=tx_both.id, amount=-60.0, category="Groceries"),
            TransactionSplit(transaction_id=tx_both.id, amount=-40.0, category="Household"),
            TransactionSplit(transaction_id=tx_split_only.id, amount=-30.0, category="Groceries"),
            TransactionSplit(transaction_id=tx_split_only.id, amount=-50.0, category="Groceries"),
        ]
    )
    db_session.commit()

    by_name = {c.name: c for c in list_categories(db_session)}
    assert by_name["Groceries"].transaction_count == 2
    assert by_name["Household"].transaction_count == 1
    assert by_name["General"].transaction_count == 1


def test_metrics_counts_rules_items_products_children_and_inactive_visibility(
    db_session: Session, seeded_account: Account
) -> None:
    created = create_category(
        db_session, CategoryCreateInput(name="Supplements Plus", amortize_period="annual")
    )
    child = create_category(
        db_session, CategoryCreateInput(name="Electrolytes", parent_id=created.id)
    )
    ids = _create_six_table_fixtures(db_session, seeded_account.id, "Supplements Plus")

    snap = get_category_snapshot(db_session, created.id)
    assert snap.transaction_count == 2
    assert snap.rule_count == 1
    assert snap.child_count == 1
    assert snap.receipt_item_count == 1
    assert snap.product_count == 1
    assert snap.has_budget_rule is True
    assert snap.budget_period == "monthly"
    assert snap.annual_limit == round(40.0 * 52.0, 2)

    annualized = update_category(db_session, created.id, CategoryUpdateInput(amortize_period="annual"))
    assert annualized.budget_period == "annual"
    assert db_session.get(BudgetRule, ids["budget_id"]).budget_period == "annual"

    cleared = update_category(db_session, created.id, CategoryUpdateInput(amortize_period=None))
    assert cleared.amortize_period is None and cleared.budget_period == "monthly"
    assert db_session.get(BudgetRule, ids["budget_id"]).budget_period == "monthly"

    promoted_child = update_category(db_session, child.id, CategoryUpdateInput(parent_id=None, is_active=False))
    assert promoted_child.parent_id is None and promoted_child.is_active is False
    assert all(c.id != child.id for c in list_categories(db_session, include_inactive=False))
    assert any(c.id == child.id for c in list_categories(db_session, include_inactive=True))


def test_deactivation_blocks_active_children_and_deactivates_budget_rule(
    db_session: Session,
) -> None:
    parent = create_category(db_session, CategoryCreateInput(name="Crafts"))
    child = create_category(
        db_session, CategoryCreateInput(name="Woodwork", parent_id=parent.id)
    )
    rule = BudgetRule(
        category="Crafts",
        weekly_limit=50.0,
        monthly_limit=216.67,
        budget_period="monthly",
        is_active=True,
    )
    db_session.add(rule)
    db_session.commit()

    with pytest.raises(CategoryValidationError):
        update_category(db_session, parent.id, CategoryUpdateInput(is_active=False))

    update_category(db_session, child.id, CategoryUpdateInput(is_active=False))
    deactivated = update_category(
        db_session, parent.id, CategoryUpdateInput(is_active=False)
    )

    assert deactivated.is_active is False
    assert deactivated.has_budget_rule is False
    db_session.refresh(rule)
    assert rule.is_active is False


def test_reparent_consistent_active_children_and_reactivation_depth_guard(
    db_session: Session,
) -> None:
    root_a = create_category(db_session, CategoryCreateInput(name="Root Alpha"))
    root_b = create_category(db_session, CategoryCreateInput(name="Root Beta"))
    child = create_category(
        db_session, CategoryCreateInput(name="Child Alpha", parent_id=root_a.id)
    )

    with pytest.raises(CategoryValidationError):
        update_category(db_session, root_a.id, CategoryUpdateInput(parent_id=root_b.id))

    update_category(db_session, child.id, CategoryUpdateInput(is_active=False))
    reparented = update_category(
        db_session, root_a.id, CategoryUpdateInput(parent_id=root_b.id)
    )
    assert reparented.parent_id == root_b.id

    with pytest.raises(CategoryValidationError):
        update_category(db_session, child.id, CategoryUpdateInput(is_active=True))


def test_create_category_syncs_preexisting_budget_rules_and_rejects_system_annual(
    db_session: Session,
) -> None:
    annual_rule = BudgetRule(
        category="annual hobbies",
        weekly_limit=20.0,
        monthly_limit=86.67,
        budget_period="annual",
        is_active=True,
    )
    monthly_rule = BudgetRule(
        category="pet gear",
        weekly_limit=15.0,
        monthly_limit=65.0,
        budget_period="monthly",
        is_active=True,
    )
    db_session.add_all([annual_rule, monthly_rule])
    db_session.commit()

    inherited = create_category(db_session, CategoryCreateInput(name="Annual Hobbies"))
    promoted = create_category(
        db_session, CategoryCreateInput(name="Pet Gear", amortize_period="annual")
    )

    assert inherited.amortize_period == "annual"
    assert inherited.budget_period == "annual"
    assert db_session.get(BudgetRule, annual_rule.id).category == "Annual Hobbies"
    assert promoted.amortize_period == "annual"
    assert db_session.get(BudgetRule, monthly_rule.id).budget_period == "annual"
    assert db_session.get(BudgetRule, monthly_rule.id).category == "Pet Gear"

    for sys_name in ("Transfers", "Income", "General", "Debt & Loans"):
        with pytest.raises(CategoryValidationError):
            create_category(
                db_session, CategoryCreateInput(name=sys_name, amortize_period="annual")
            )


def test_package_preserves_hierarchy_helpers_and_error_types(
    db_session: Session,
) -> None:
    parent_map = get_category_parent_map(db_session)
    assert get_parent_category("Dining", parent_map) == "Food"
    assert get_root_category("Dining", parent_map) == "Food"
    assert "dining" in get_descendant_category_names("Food", parent_map)
    assert "Transfers" in get_canonical_category_names(db_session)
    assert "Transfers" not in get_canonical_category_names(
        db_session, include_system_non_expense=False
    )

    with pytest.raises(CategoryNotFoundError):
        get_category_snapshot(db_session, 999999)
    with pytest.raises(CategoryConflictError):
        create_category(db_session, CategoryCreateInput(name="food"))


def test_budget_sync_canonicalizes_syncs_amortization_and_rejects_system_annual(
    db_session: Session,
) -> None:
    resolved = resolve_budget_rule_category(
        db_session, "  groceries ", budget_period="monthly"
    )
    assert resolved == "Groceries"
    assert resolve_budget_rule_category(db_session, "Education", budget_period="annual") == "Education"

    travel_cat = db_session.scalar(select(Category).where(Category.name == "Travel"))
    assert travel_cat is not None and travel_cat.amortize_period is None

    canonical = sync_budget_rule_amortization(
        db_session, category_name=" travel ", budget_period="annual", is_active=True
    )
    assert canonical == "Travel"
    assert travel_cat.amortize_period == "annual"

    # Inactive rule updates must ignore (neither set nor clear) Category.amortize_period
    sync_budget_rule_amortization(
        db_session, category_name="Travel", budget_period="monthly", is_active=False
    )
    assert travel_cat.amortize_period == "annual"

    food_cat = db_session.scalar(select(Category).where(Category.name == "Food"))
    assert food_cat is not None and food_cat.amortize_period is None
    sync_budget_rule_amortization(
        db_session, category_name="Food", budget_period="annual", is_active=False
    )
    assert food_cat.amortize_period is None

    sync_budget_rule_amortization(
        db_session, category_name="Travel", budget_period="monthly", is_active=True
    )
    assert travel_cat.amortize_period is None

    assert (
        sync_budget_rule_amortization(
            db_session, category_name="Debt & Loans", budget_period="monthly", is_active=True
        )
        == "Debt & Loans"
    )

    for sys_name in ("Transfers", "Income", "General", "Debt & Loans"):
        with pytest.raises(CategoryValidationError):
            resolve_budget_rule_category(db_session, sys_name, budget_period="annual")
        with pytest.raises(CategoryValidationError):
            sync_budget_rule_amortization(
                db_session, category_name=sys_name, budget_period="annual", is_active=True
            )

    with pytest.raises(CategoryValidationError):
        sync_budget_rule_amortization(
            db_session, category_name="   ", budget_period="monthly", is_active=True
        )


def test_get_active_categories_fallback_chain_and_exclusions() -> None:
    session_factory, engine = _new_memory_session_factory()
    try:
        # 1. Empty DB -> filtered DEFAULT_CATEGORY_NAMES (keeps Debt & Loans, excludes Transfers/Income/General)
        with session_factory() as db:
            defaults = get_active_categories(db)
            assert "Debt & Loans" in defaults
            assert "Groceries" in defaults
            assert {"Transfers", "Income", "General"}.isdisjoint(defaults)

            # 2. Transaction fallback when Category and BudgetRule tables are empty
            acc = Account(
                name="Everyday",
                account_type="checking",
                tier="cash",
                institution="ANZ",
                currency="NZD",
            )
            db.add(acc)
            db.flush()
            db.add_all(
                [
                    Transaction(
                        transaction_date=date.today(),
                        account_id=acc.id,
                        amount=-10.0,
                        currency="NZD",
                        merchant="Cafe",
                        category="Dining",
                        raw_hash="active-cat-tx-1",
                    ),
                    Transaction(
                        transaction_date=date.today(),
                        account_id=acc.id,
                        amount=-50.0,
                        currency="NZD",
                        merchant="Unknown",
                        category="General",
                        raw_hash="active-cat-tx-2",
                    ),
                ]
            )
            db.commit()
            assert get_active_categories(db) == ["Dining"]

            # 3. BudgetRule fallback overrides Transaction fallback
            db.add(
                BudgetRule(
                    category="Travel",
                    weekly_limit=50.0,
                    monthly_limit=216.67,
                    is_active=True,
                )
            )
            db.commit()
            assert get_active_categories(db) == ["Travel"]

            # 4. Seeded Category table overrides BudgetRule fallback and retains Debt & Loans
            ensure_default_categories(db)
            cats = get_active_categories(db)
            assert "Debt & Loans" in cats
            assert "Groceries" in cats
            assert {"Transfers", "Income", "General"}.isdisjoint(cats)
    finally:
        Base.metadata.drop_all(bind=engine)


def test_annual_category_taxonomy_detached_and_immutable() -> None:
    session_factory, engine = _new_memory_session_factory()
    try:
        with session_factory() as db:
            ensure_default_categories(db)
            vehicle = db.scalar(select(Category).where(Category.name == "Vehicle"))
            assert vehicle is not None
            update_category(db, vehicle.id, CategoryUpdateInput(amortize_period="annual"))
            db.add(
                BudgetRule(
                    category="Education",
                    weekly_limit=25.0,
                    monthly_limit=108.33,
                    budget_period="annual",
                    is_active=True,
                )
            )
            db.commit()
            taxonomy = get_annual_category_taxonomy(db)

        assert isinstance(taxonomy, AnnualCategoryTaxonomy)
        assert taxonomy.parent_map["Vehicle"] == "Transport"
        assert taxonomy.lower_to_parent["vehicle"] == "transport"
        assert taxonomy.canonical_name_map["education"] == "Education"
        assert taxonomy.canonical_name_map["vehicle"] == "Vehicle"
        assert "vehicle" in taxonomy.annual_cats
        assert "education" in taxonomy.annual_cats
        assert "education" in taxonomy.annual_base_roots
        assert "vehicle" not in taxonomy.annual_base_roots
        with pytest.raises(TypeError):
            taxonomy.parent_map["Education"] = "Food"  # type: ignore[index]
        with pytest.raises(FrozenInstanceError):
            taxonomy.annual_cats = frozenset()  # type: ignore[misc]
    finally:
        Base.metadata.drop_all(bind=engine)


def test_normalize_existing_legacy_categories_idempotent_with_inactive_category(
    db_session: Session, seeded_account: Account
) -> None:
    # Deactivating a default category should remain idempotent across repeated runs
    alc = db_session.scalar(select(Category).where(Category.name == "Alcohol"))
    assert alc is not None
    update_category(db_session, alc.id, CategoryUpdateInput(is_active=False))
    db_session.add(
        Transaction(
            transaction_date=date.today(),
            account_id=seeded_account.id,
            amount=-20.0,
            currency="NZD",
            merchant="Bottle Store",
            category="Alcohol",
            raw_hash="inactive-cat-alc",
            tags="[]",
        )
    )
    db_session.commit()
    assert normalize_existing_legacy_categories(db_session) == 0
