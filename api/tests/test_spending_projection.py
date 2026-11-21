"""SQLite :memory: interface tests for Spending Projection core and /budgets/breakdown parity."""

from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import (
    Account,
    BudgetRule,
    Loan,
    Receipt,
    ReceiptItem,
    Transaction,
    TransactionSplit,
)
from app.services.category_taxonomy import ensure_default_categories
from app.services.spending_projection import (
    InvalidWindowError,
    ReportingWindow,
    expand_expense_lines,
    get_active_recurring_commitment_allocations,
    project_window,
    resolve_reporting_window,
)


@pytest.fixture()
def memory_db() -> Iterator[Session]:
    """Create an isolated SQLite :memory: session with default Plutus taxonomy seeded."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = testing_session_local()
    ensure_default_categories(db)
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def api_client(memory_db: Session) -> Iterator[TestClient]:
    """Create a FastAPI TestClient bound to the isolated SQLite :memory: session."""

    def _override_get_db() -> Iterator[Session]:
        yield memory_db

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def _seed_account(db: Session, name: str = "ANZ Current", currency: str = "NZD") -> Account:
    acct = Account(
        name=name,
        institution="ANZ",
        account_type="cheque",
        currency=currency,
    )
    db.add(acct)
    db.commit()
    db.refresh(acct)
    return acct


def test_transaction_split_signed_amount_ownership(memory_db: Session) -> None:
    """TransactionSplit.signed_amount() resolves positive, negative, and mixed-sign splits."""
    acct = _seed_account(memory_db)

    # Case 1: All-positive splits under a negative outflow parent are negated.
    tx_pos_splits = Transaction(
        account_id=acct.id,
        transaction_date=date(2026, 11, 10),
        merchant="Supermarket",
        amount=-100.0,
        currency="NZD",
        category="Groceries",
        raw_hash="hash-pos-splits-1",
    )
    s1 = TransactionSplit(amount=60.0, category="Groceries")
    s2 = TransactionSplit(amount=40.0, category="Household Supplies")
    tx_pos_splits.splits = [s1, s2]
    memory_db.add(tx_pos_splits)
    memory_db.commit()
    memory_db.refresh(tx_pos_splits)

    assert [s.signed_amount() for s in tx_pos_splits.splits] == [-60.0, -40.0]

    # Case 2: Mixed-sign splits under a negative outflow parent preserve explicit signs.
    tx_mixed = Transaction(
        account_id=acct.id,
        transaction_date=date(2026, 11, 11),
        merchant="Dinner Split",
        amount=-50.0,
        currency="NZD",
        category="Restaurants",
        raw_hash="hash-mixed-splits-2",
    )
    m1 = TransactionSplit(amount=-80.0, category="Restaurants")
    m2 = TransactionSplit(amount=30.0, category="Income")
    tx_mixed.splits = [m1, m2]
    memory_db.add(tx_mixed)
    memory_db.commit()
    memory_db.refresh(tx_mixed)

    assert [s.signed_amount() for s in tx_mixed.splits] == [-80.0, 30.0]
    # Explicit override parameters also work on detached splits.
    detached = TransactionSplit(amount=25.0, category="Coffee")
    assert detached.signed_amount(parent_amount=-25.0, siblings=[detached]) == -25.0


def test_reporting_window_validation_and_reference_date(memory_db: Session) -> None:
    """ReportingWindow validates period/date bounds and resolves deterministically from reference_date."""
    ref = date(2026, 11, 18)  # Wednesday

    wk = resolve_reporting_window(ReportingWindow(period="week"), reference_date=ref)
    assert wk.start_date == date(2026, 11, 16)
    assert wk.end_date == date(2026, 11, 22)
    assert wk.is_weekly_cadence is True
    assert wk.is_budget_cadence_window is True

    last_wk = resolve_reporting_window(ReportingWindow(period="last_week"), reference_date=ref)
    assert last_wk.start_date == date(2026, 11, 9)
    assert last_wk.end_date == date(2026, 11, 15)
    assert last_wk.as_of_date == date(2026, 11, 15)
    assert last_wk.is_weekly_cadence is True

    mo = resolve_reporting_window(ReportingWindow(period="month"), reference_date=ref)
    assert mo.start_date == date(2026, 11, 1)
    assert mo.end_date == date(2026, 11, 18)
    assert mo.is_monthly_cadence is True

    last_mo = resolve_reporting_window(ReportingWindow(period="last_month"), reference_date=ref)
    assert last_mo.start_date == date(2026, 10, 1)
    assert last_mo.end_date == date(2026, 10, 31)
    assert last_mo.is_monthly_cadence is True

    custom = resolve_reporting_window(
        ReportingWindow(period="month", start_date=date(2026, 11, 1), end_date=date(2026, 11, 10)),
        reference_date=ref,
    )
    assert custom.period_label == "custom"
    assert custom.effective_period is None
    assert custom.is_budget_cadence_window is False

    with pytest.raises(InvalidWindowError, match="Invalid period"):
        resolve_reporting_window(ReportingWindow(period="fortnight"), reference_date=ref)

    with pytest.raises(InvalidWindowError, match="start_date must be on or before end_date"):
        resolve_reporting_window(
            ReportingWindow(start_date=date(2026, 11, 20), end_date=date(2026, 11, 10)),
            reference_date=ref,
        )

    with pytest.raises(InvalidWindowError, match="group_by must be 'leaf' or 'base'"):
        project_window(
            memory_db,
            ReportingWindow(period="month"),
            group_by="invalid",  # type: ignore[arg-type]
            reference_date=ref,
        )


def test_last_week_uses_weekly_budget_cadence_limits(memory_db: Session) -> None:
    """Bug 1 fix: period='last_week' compares category spend against weekly_limit, not monthly_limit."""
    acct = _seed_account(memory_db)
    ref = date(2026, 11, 18)  # last_week is 2026-11-09 .. 2026-11-15

    memory_db.add(
        BudgetRule(
            category="Groceries",
            weekly_limit=100.0,
            monthly_limit=433.33,
            budget_period="weekly",
            is_active=True,
        )
    )
    memory_db.add(
        Transaction(
            account_id=acct.id,
            transaction_date=date(2026, 11, 12),
            merchant="Countdown",
            amount=-125.0,
            currency="NZD",
            category="Groceries",
            raw_hash="hash-last-week-groc",
        )
    )
    memory_db.commit()

    proj = project_window(
        memory_db,
        ReportingWindow(period="last_week"),
        group_by="leaf",
        reference_date=ref,
    )
    groc = next(c for c in proj.categories if c.category == "Groceries")
    assert groc.budget_limit == 100.0
    assert groc.percent_used == 125.0
    assert groc.is_over_budget is True
    assert groc.warning_level == "danger"
    assert proj.over_budget_count == 1


def test_transfers_receipt_lines_excluded_uniformly(memory_db: Session) -> None:
    """Bug 2 fix: Receipt items categorized as Transfers are excluded from expense lines and projection spend."""
    acct = _seed_account(memory_db)
    ref = date(2026, 11, 18)

    tx = Transaction(
        account_id=acct.id,
        transaction_date=date(2026, 11, 10),
        merchant="Mixed Superstore",
        amount=-100.0,
        currency="NZD",
        category="Groceries",
        raw_hash="hash-mixed-superstore",
    )
    memory_db.add(tx)
    memory_db.flush()

    receipt = Receipt(
        transaction_id=tx.id,
        merchant="Mixed Superstore",
        receipt_date=date(2026, 11, 10),
        total_amount=100.0,
        currency="NZD",
        status="matched",
    )
    memory_db.add(receipt)
    memory_db.flush()

    memory_db.add_all(
        [
            ReceiptItem(
                receipt_id=receipt.id,
                item_name="Produce",
                unit_price=60.0,
                total_price=60.0,
                category="Groceries",
            ),
            ReceiptItem(
                receipt_id=receipt.id,
                item_name="Cash Out / Transfer",
                unit_price=40.0,
                total_price=40.0,
                category="Transfers",
            ),
        ]
    )
    memory_db.commit()
    memory_db.refresh(tx)

    lines = expand_expense_lines(tx)
    assert len(lines) == 1
    assert lines[0].category == "Groceries"
    assert lines[0].amount_nzd == 60.0

    proj = project_window(
        memory_db,
        ReportingWindow(period="month"),
        group_by="leaf",
        reference_date=ref,
    )
    assert proj.total_spend == 60.0
    cat_names = {c.category for c in proj.categories}
    assert "Groceries" in cat_names
    assert "Transfers" not in cat_names


def test_per_split_recurring_commitments_attribution(memory_db: Session) -> None:
    """Bug 3 fix: Recurring transaction with manual splits attributes commitments per split category."""
    acct = _seed_account(memory_db)
    ref = date(2026, 11, 18)

    lent_loan = Loan(
        name="Friend Loan",
        lender="Alex",
        loan_type="lent",
        principal_amount=500.0,
        annual_interest_rate=0.0,
        term_months=12,
        monthly_payment=0.0,
        currency="NZD",
        start_date=date(2026, 1, 1),
        is_active=True,
    )
    memory_db.add(lent_loan)
    memory_db.flush()

    # Recurring quarterly charge on 2026-10-01 (outside Nov month window, inside 95-day quarterly lookback).
    tx = Transaction(
        account_id=acct.id,
        transaction_date=date(2026, 10, 1),
        merchant="Multi-Service Utility",
        amount=-350.0,
        currency="NZD",
        category="General",
        recurrence="quarterly",
        raw_hash="hash-multi-utility",
    )
    tx.splits = [
        TransactionSplit(amount=-195.0, category="Electricity"),
        TransactionSplit(amount=-105.0, category="Internet"),
        TransactionSplit(amount=-30.0, category="Transfers"),
        TransactionSplit(amount=-20.0, category="Loans", loan_id=lent_loan.id),
    ]
    memory_db.add(tx)
    memory_db.commit()

    allocs = get_active_recurring_commitment_allocations(memory_db, ref)
    by_cat = {a.category: a for a in allocs}
    assert set(by_cat.keys()) == {"Electricity", "Internet"}
    assert by_cat["Electricity"].original_amount == 195.0
    assert by_cat["Electricity"].weekly_nzd == 15.0  # 195 * 4 / 52
    assert by_cat["Electricity"].monthly_nzd == 65.0  # 15 * 52 / 12
    assert by_cat["Internet"].original_amount == 105.0
    assert by_cat["Internet"].weekly_nzd == 8.08
    assert by_cat["Internet"].monthly_nzd == 35.01

    proj = project_window(
        memory_db,
        ReportingWindow(period="month"),
        group_by="leaf",
        reference_date=ref,
    )
    proj_cats = {c.category: c for c in proj.categories}
    assert "General" not in proj_cats
    assert proj_cats["Electricity"].total_spend == 65.0
    assert proj_cats["Electricity"].amortized_count == 1
    assert proj_cats["Internet"].total_spend == 35.01
    assert proj_cats["Internet"].amortized_count == 1


def test_annualized_category_single_pass_includes_recurring_and_respects_window_type(
    memory_db: Session,
) -> None:
    """Bug 4 fix: Annualized Category 365-day smoothing includes recurring items on week/month presets only."""
    acct = _seed_account(memory_db)
    ref = date(2026, 11, 18)

    memory_db.add(
        BudgetRule(
            category="Dental",
            weekly_limit=23.08,
            monthly_limit=100.0,
            budget_period="annual",
            is_active=True,
        )
    )
    # One non-recurring dental charge 4 months ago + one recurring annual dental charge in current month.
    memory_db.add_all(
        [
            Transaction(
                account_id=acct.id,
                transaction_date=date(2026, 7, 10),
                merchant="Auckland Dental",
                amount=-600.0,
                currency="NZD",
                category="Dental",
                recurrence=None,
                raw_hash="hash-dental-july",
            ),
            Transaction(
                account_id=acct.id,
                transaction_date=date(2026, 11, 5),
                merchant="Dental Plan Annual",
                amount=-600.0,
                currency="NZD",
                category="Dental",
                recurrence="annual",
                raw_hash="hash-dental-nov-annual",
            ),
        ]
    )
    memory_db.commit()

    # Monthly window: 365-day total is $1200 across 2 transactions; monthly equivalent contribution is $100.
    month_proj = project_window(
        memory_db,
        ReportingWindow(period="month"),
        group_by="leaf",
        reference_date=ref,
    )
    assert month_proj.total_spend == 100.0  # 1200 / 12 (no double-injection from amortized recurrence)
    dental_mo = next(c for c in month_proj.categories if c.category == "Dental")
    assert dental_mo.total_spend == 1200.0
    assert dental_mo.transaction_count == 2
    assert dental_mo.actual_transaction_count == 2
    assert dental_mo.amortized_count == 0
    assert dental_mo.budget_period == "annual"

    # Custom date window (2026-11-01 .. 2026-11-18): no 365-day smoothing; reports actual in-window $600 spend.
    custom_proj = project_window(
        memory_db,
        ReportingWindow(start_date=date(2026, 11, 1), end_date=date(2026, 11, 18)),
        group_by="leaf",
        reference_date=ref,
    )
    assert custom_proj.total_spend == 600.0
    dental_custom = next(c for c in custom_proj.categories if c.category == "Dental")
    assert dental_custom.total_spend == 600.0
    assert dental_custom.transaction_count == 1
    assert dental_custom.actual_transaction_count == 1

    # All-time window: no 365-day smoothing; reports actual all-time spend ($1200).
    all_proj = project_window(
        memory_db,
        ReportingWindow(period="all"),
        group_by="leaf",
        reference_date=ref,
    )
    assert all_proj.total_spend == 1200.0
    dental_all = next(c for c in all_proj.categories if c.category == "Dental")
    assert dental_all.total_spend == 1200.0
    assert dental_all.transaction_count == 2


def test_budgets_breakdown_http_400_and_midas_json_parity(
    api_client: TestClient,
    memory_db: Session,
) -> None:
    """GET /budgets/breakdown translates InvalidWindowError to HTTP 400 and preserves Midas JSON parity."""
    acct = _seed_account(memory_db)
    memory_db.add(
        BudgetRule(
            category="Groceries",
            weekly_limit=150.0,
            monthly_limit=650.0,
            budget_period="weekly",
            is_active=True,
        )
    )
    memory_db.add(
        Transaction(
            account_id=acct.id,
            transaction_date=date.today(),
            merchant="Pak n Save",
            amount=-120.0,
            currency="NZD",
            category="Groceries",
            raw_hash="hash-paknsave-http",
        )
    )
    memory_db.commit()

    bad_period = api_client.get("/budgets/breakdown?period=invalid_period")
    assert bad_period.status_code == 400
    assert "Invalid period" in bad_period.json()["detail"]

    bad_group = api_client.get("/budgets/breakdown?group_by=invalid")
    assert bad_group.status_code == 400
    assert "group_by must be 'leaf' or 'base'" in bad_group.json()["detail"]

    bad_dates = api_client.get("/budgets/breakdown?start_date=2026-11-20&end_date=2026-11-10")
    assert bad_dates.status_code == 400
    assert "start_date must be on or before end_date" in bad_dates.json()["detail"]

    ok = api_client.get("/budgets/breakdown?period=week&group_by=leaf")
    assert ok.status_code == 200
    payload = ok.json()
    assert set(payload.keys()) == {
        "period",
        "start_date",
        "end_date",
        "total_spend",
        "total_income",
        "total_transfers",
        "net_savings",
        "categories",
        "over_budget_count",
    }
    assert payload["period"] == "week"
    assert payload["total_spend"] == 120.0
    groc = next(c for c in payload["categories"] if c["category"] == "Groceries")
    assert groc["budget_limit"] == 150.0
    assert groc["percent_used"] == 80.0
    assert groc["warning_level"] == "warning"
    assert groc["parent_category"] == "Food"
