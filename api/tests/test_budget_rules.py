"""SQLite :memory: interface tests for the deep Budget Rules lifecycle package."""

from collections.abc import Iterator
from dataclasses import FrozenInstanceError, is_dataclass
from datetime import date

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Account, BudgetRule, Category, Transaction
from app.services.budget_rules import (
    DEFAULT_BASELINE_BUDGETS,
    DEFAULT_WEEKLY_BUDGET_LIMIT_NZD,
    BudgetRuleNotFoundError,
    BudgetRuleSnapshot,
    BudgetRuleUpsertInput,
    BudgetRuleValidationError,
    delete_budget_rule,
    initialize_default_budgets,
    list_budget_rules,
    resolve_budget_limits,
    sync_budget_rules_from_burn,
    upsert_budget_rule,
)
from app.services.category_taxonomy import ensure_default_categories, load_categories_by_lower


def _new_memory_session_factory() -> tuple[sessionmaker[Session], object]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine), engine


@pytest.fixture()
def db_with_engine() -> Iterator[tuple[Session, object]]:
    """Provide an isolated SQLite :memory: session and engine with default categories."""
    session_factory, engine = _new_memory_session_factory()
    db = session_factory()
    ensure_default_categories(db)
    try:
        yield db, engine
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session(db_with_engine: tuple[Session, object]) -> Session:
    """Return the isolated Session from db_with_engine."""
    return db_with_engine[0]


def _capture_sql(engine: object, fn):
    statements: list[str] = []

    def _listener(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.strip().upper())

    event.listen(engine, "before_cursor_execute", _listener)
    try:
        result = fn()
    finally:
        event.remove(engine, "before_cursor_execute", _listener)
    return result, statements


def _count_statements(statements: list[str], prefix: str) -> int:
    return sum(1 for stmt in statements if stmt.startswith(prefix))


def test_snapshot_is_frozen_and_detached_after_session_close() -> None:
    session_factory, engine = _new_memory_session_factory()
    db = session_factory()
    ensure_default_categories(db)
    created = upsert_budget_rule(
        db,
        BudgetRuleUpsertInput(category="groceries", weekly_limit=210.0),
    )
    listed = list_budget_rules(db)
    db.close()
    Base.metadata.drop_all(bind=engine)

    assert is_dataclass(created)
    assert isinstance(created, BudgetRuleSnapshot)
    assert created.category == "Groceries"
    assert isinstance(created.weekly_limit, float)
    assert isinstance(created.monthly_limit, float)
    assert created.annual_limit == round(210.0 * 52.0, 2)
    assert listed == (created,)

    with pytest.raises(FrozenInstanceError):
        created.weekly_limit = 999.0  # type: ignore[misc]


def test_resolve_budget_limits_pairing_and_validation() -> None:
    default_w, default_m = resolve_budget_limits(None, None)
    assert default_w == DEFAULT_WEEKLY_BUDGET_LIMIT_NZD
    assert default_m == 433.33

    w_only, m_from_w = resolve_budget_limits(200.0, None)
    assert (w_only, m_from_w) == (200.0, 866.67)

    w_from_m, m_only = resolve_budget_limits(None, 260.0)
    assert (w_from_m, m_only) == (60.0, 260.0)

    w_ann, m_ann = resolve_budget_limits(None, None, annual=2600.0)
    assert (w_ann, m_ann) == (50.0, 216.67)

    w_ovr, m_ovr = resolve_budget_limits(75.0, 300.0, annual=2600.0)
    assert (w_ovr, m_ovr) == (75.0, 300.0)

    assert resolve_budget_limits(0.0, 0.0) == (0.0, 0.0)

    invalid_values = (-1.0, float("nan"), float("inf"), float("-inf"), True, False)
    for bad in invalid_values:
        with pytest.raises(BudgetRuleValidationError):
            resolve_budget_limits(bad, None)
        with pytest.raises(BudgetRuleValidationError):
            resolve_budget_limits(None, bad)
        with pytest.raises(BudgetRuleValidationError):
            resolve_budget_limits(None, None, annual=bad)


def test_upsert_and_sync_validation_guards(db_session: Session) -> None:
    for bad_cat in ("", "   ", "X" * 51):
        with pytest.raises(BudgetRuleValidationError):
            upsert_budget_rule(
                db_session,
                BudgetRuleUpsertInput(category=bad_cat, weekly_limit=50.0),
            )

    with pytest.raises(BudgetRuleValidationError):
        upsert_budget_rule(
            db_session,
            BudgetRuleUpsertInput(category="Food", weekly_limit=50.0, budget_period="daily"),
        )

    for sys_cat in ("Transfers", "Income", "General", "Debt & Loans"):
        with pytest.raises(BudgetRuleValidationError):
            upsert_budget_rule(
                db_session,
                BudgetRuleUpsertInput(
                    category=sys_cat,
                    annual_limit=520.0,
                    budget_period="annual",
                ),
            )

    for bad_weeks in (0, 53, True):
        with pytest.raises(BudgetRuleValidationError):
            sync_budget_rules_from_burn(db_session, lookback_weeks=bad_weeks)


def test_upsert_amortization_sync_and_delete_preserves_category(db_session: Session) -> None:
    travel = upsert_budget_rule(
        db_session,
        BudgetRuleUpsertInput(
            category=" travel ",
            annual_limit=2600.0,
            budget_period="annual",
        ),
    )
    assert travel.category == "Travel"
    assert travel.budget_period == "annual"
    travel_cat = db_session.scalar(select(Category).where(Category.name == "Travel"))
    assert travel_cat is not None
    assert travel_cat.amortize_period == "annual"

    # Inactive update preserves existing Category.amortize_period without clearing it
    inactive = upsert_budget_rule(
        db_session,
        BudgetRuleUpsertInput(
            category="TRAVEL",
            monthly_limit=200.0,
            budget_period="monthly",
            is_active=False,
        ),
    )
    assert inactive.category == "Travel"
    assert inactive.is_active is False
    db_session.refresh(travel_cat)
    assert travel_cat.amortize_period == "annual"

    # Active and all filtering
    assert list_budget_rules(db_session, is_active=True) == ()
    assert list_budget_rules(db_session, is_active=False) == (inactive,)
    assert list_budget_rules(db_session, is_active=None) == (inactive,)

    # Delete preserves Category.amortize_period='annual'
    delete_budget_rule(db_session, travel.id)
    assert list_budget_rules(db_session, is_active=None) == ()
    db_session.refresh(travel_cat)
    assert travel_cat.amortize_period == "annual"

    with pytest.raises(BudgetRuleNotFoundError):
        delete_budget_rule(db_session, travel.id)


def test_initialize_default_budgets_is_batched_and_idempotent(
    db_with_engine: tuple[Session, object],
) -> None:
    db, engine = db_with_engine
    ins_cat = db.scalar(select(Category).where(Category.name == "Insurance"))
    assert ins_cat is not None
    ins_cat.amortize_period = "annual"
    db.commit()

    seeded, cold_statements = _capture_sql(engine, lambda: initialize_default_budgets(db))
    assert len(seeded) == len(DEFAULT_BASELINE_BUDGETS)
    assert _count_statements(cold_statements, "SELECT") <= 3

    by_cat = {rule.category: rule for rule in seeded}
    assert by_cat["Insurance"].budget_period == "annual"
    assert by_cat["Groceries"].weekly_limit == 200.0

    # Idempotent second call executes <= 1 SELECT and 0 INSERT/UPDATE statements
    second, warm_statements = _capture_sql(engine, lambda: initialize_default_budgets(db))
    assert second == seeded
    assert _count_statements(warm_statements, "SELECT") <= 1
    assert _count_statements(warm_statements, "INSERT") == 0
    assert _count_statements(warm_statements, "UPDATE") == 0

    db.refresh(ins_cat)
    assert ins_cat.amortize_period == "annual"


def test_load_categories_by_lower_empty_and_batched(
    db_with_engine: tuple[Session, object],
) -> None:
    db, engine = db_with_engine
    empty_res, empty_stmts = _capture_sql(
        engine, lambda: load_categories_by_lower(db, ["", "   "])
    )
    assert empty_res == {}
    assert _count_statements(empty_stmts, "SELECT") == 0

    loaded, batch_stmts = _capture_sql(
        engine, lambda: load_categories_by_lower(db, [" groceries ", "TRAVEL"])
    )
    assert set(loaded.keys()) == {"groceries", "travel"}
    assert loaded["groceries"].name == "Groceries"
    assert loaded["travel"].name == "Travel"
    assert _count_statements(batch_stmts, "SELECT") == 1


def test_sync_budget_rules_from_burn_canonicalizes_and_batches(
    db_with_engine: tuple[Session, object],
) -> None:
    db, engine = db_with_engine
    account = Account(
        name="Everyday Checking",
        account_type="checking",
        tier="cash",
        institution="Kiwibank",
        currency="NZD",
    )
    db.add(account)
    db.flush()

    travel_cat = db.scalar(select(Category).where(Category.name == "Travel"))
    assert travel_cat is not None
    travel_cat.amortize_period = "annual"

    db.add(
        BudgetRule(
            category="travel",
            weekly_limit=10.0,
            monthly_limit=43.33,
            budget_period="monthly",
            is_active=False,
        )
    )
    today = date.today()
    db.add_all(
        [
            Transaction(
                transaction_date=today,
                account_id=account.id,
                amount=-520.0,
                currency="NZD",
                merchant="Southern Airways",
                category="Travel",
                raw_hash="burn-sync-travel",
            ),
            Transaction(
                transaction_date=today,
                account_id=account.id,
                amount=-160.0,
                currency="NZD",
                merchant="Corner Grocer",
                category="Groceries",
                raw_hash="burn-sync-groceries",
            ),
        ]
    )
    db.commit()

    synced, stmts = _capture_sql(
        engine,
        lambda: sync_budget_rules_from_burn(db, lookback_weeks=4, reference_date=today),
    )
    by_cat = {rule.category: rule for rule in synced}
    assert set(by_cat.keys()) == {"Groceries", "Travel"}
    assert by_cat["Travel"].budget_period == "annual"
    assert by_cat["Travel"].is_active is True
    assert by_cat["Travel"].weekly_limit == pytest.approx(10.0, abs=0.01)
    assert by_cat["Groceries"].budget_period == "monthly"
    assert by_cat["Groceries"].weekly_limit == pytest.approx(40.0, abs=0.01)
    assert _count_statements(stmts, "INSERT") == 1
    assert _count_statements(stmts, "UPDATE") == 1

    db.refresh(travel_cat)
    assert travel_cat.amortize_period == "annual"
