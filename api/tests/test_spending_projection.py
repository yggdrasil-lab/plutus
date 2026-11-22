"""SQLite :memory: interface tests for Spending Projection core and /budgets/breakdown parity."""

from collections.abc import Iterator
from dataclasses import FrozenInstanceError, is_dataclass
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import (
    Account,
    BudgetRule,
    Category,
    Loan,
    Receipt,
    ReceiptItem,
    Transaction,
    TransactionSplit,
)
from app.services.category_taxonomy import ensure_default_categories
from app.services.fx_service import to_nzd_amount
from app.services.spending_projection import (
    InvalidWindowError,
    ProjectedSplit,
    ProjectedTransaction,
    ReportingWindow,
    TransactionProjection,
    TransactionQuery,
    expand_expense_lines,
    get_active_recurring_commitment_allocations,
    project_cashflow_transactions,
    project_transactions,
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


def _effective_cashflow_amount_nzd(item: ProjectedTransaction) -> float:
    """Compute Reporting Currency NZD magnitude for one detached ProjectedTransaction."""
    return round(to_nzd_amount(abs(item.amount), item.currency, item.exchange_rate), 2)


def test_project_cashflow_transactions_validation_and_exact_window_parity(
    memory_db: Session,
) -> None:
    """project_cashflow_transactions enforces invariants and reconciles with project_window."""
    ref = date(2026, 11, 18)  # Wednesday; week=Nov16..22, last_week=Nov9..15, month=Nov1..18, last_month=Oct1..31
    checking = _seed_account(memory_db, name="Primary Checking", currency="NZD")
    sgd_acct = _seed_account(memory_db, name="SGD Cash", currency="SGD")

    lent_loan = Loan(
        name="Loan to Alex",
        lender="Alex Tan",
        loan_type="lent",
        principal_amount=2000.0,
        annual_interest_rate=0.0,
        term_months=0,
        monthly_payment=0.0,
        currency="NZD",
        start_date=date(2026, 1, 1),
        is_active=True,
    )
    borrowed_loan = Loan(
        name="Car Finance",
        lender="North Bank",
        loan_type="borrowed",
        principal_amount=5000.0,
        annual_interest_rate=5.0,
        term_months=24,
        monthly_payment=250.0,
        currency="NZD",
        start_date=date(2026, 1, 1),
        is_active=True,
    )
    memory_db.add_all([lent_loan, borrowed_loan])
    memory_db.flush()

    memory_db.add(
        BudgetRule(
            category="Dental",
            weekly_limit=10.0,
            monthly_limit=43.33,
            budget_period="annual",
            is_active=True,
        )
    )

    # 1. Standard salary inflow + payslip receipt
    salary = Transaction(
        account_id=checking.id,
        transaction_date=date(2026, 11, 17),
        merchant="Acme Payroll",
        amount=3000.0,
        currency="NZD",
        category="Income",
        raw_hash="cf-salary-nov17",
    )
    # 2. Mixed-sign split on net-negative parent (-50 = -80 Dining + 30 Income)
    mixed_neg = Transaction(
        account_id=checking.id,
        transaction_date=date(2026, 11, 16),
        merchant="Dinner Refund Split",
        amount=-50.0,
        currency="NZD",
        category="Dining",
        tags='["social"]',
        raw_hash="cf-mixed-neg-nov16",
    )
    mixed_neg.splits = [
        TransactionSplit(amount=-80.0, category="Dining", tags='["dinner"]'),
        TransactionSplit(amount=30.0, category="Income", tags='["split-back"]'),
    ]
    # 3. Mixed-sign split on net-positive parent (+137.17 = +1000 lent + -862.83 Travel)
    mixed_pos = Transaction(
        account_id=checking.id,
        transaction_date=date(2026, 11, 12),
        merchant="Travel Reimbursement + Ticket",
        amount=137.17,
        currency="NZD",
        category="General",
        raw_hash="cf-mixed-pos-nov12",
    )
    mixed_pos.splits = [
        TransactionSplit(amount=1000.0, category="Debt & Loans", loan_id=lent_loan.id),
        TransactionSplit(amount=-862.83, category="Travel", tags='["trip"]'),
    ]
    # 4. Partial lent repayment in SGD (150 SGD * 1.30 = 195 NZD; 100 SGD lent -> 50 SGD = 65 NZD income)
    partial_sgd = Transaction(
        account_id=sgd_acct.id,
        transaction_date=date(2026, 10, 20),
        merchant="Alex Partial Return",
        amount=150.0,
        currency="SGD",
        exchange_rate=1.30,
        category="Debt & Loans",
        loan_id=lent_loan.id,
        loan_payment_amount=100.0,
        raw_hash="cf-partial-sgd-oct20",
    )
    # 5. Pure lent repayment (loan_payment_amount is None -> 0 income) & lent disbursement
    pure_repay = Transaction(
        account_id=checking.id,
        transaction_date=date(2026, 11, 18),
        merchant="Alex Pure Repayment",
        amount=200.0,
        currency="NZD",
        category="Debt & Loans",
        loan_id=lent_loan.id,
        raw_hash="cf-pure-repay-nov18",
    )
    lent_out = Transaction(
        account_id=checking.id,
        transaction_date=date(2026, 11, 18),
        merchant="Advance to Alex",
        amount=-400.0,
        currency="NZD",
        category="Debt & Loans",
        loan_id=lent_loan.id,
        raw_hash="cf-lent-out-nov18",
    )
    borrowed_out = Transaction(
        account_id=checking.id,
        transaction_date=date(2026, 11, 17),
        merchant="Car Loan Repayment",
        amount=-250.0,
        currency="NZD",
        category="Debt & Loans",
        loan_id=borrowed_loan.id,
        raw_hash="cf-borrowed-out-nov17",
    )
    # 6. Transfer parent (always excluded) + mixed receipt (Groceries 60 + Transfers 40)
    transfer_parent = Transaction(
        account_id=checking.id,
        transaction_date=date(2026, 11, 17),
        merchant="Internal Transfer",
        amount=-500.0,
        currency="NZD",
        category="Transfers",
        raw_hash="cf-xfer-nov17",
    )
    mixed_receipt_tx = Transaction(
        account_id=checking.id,
        transaction_date=date(2026, 11, 17),
        merchant="North Shore Mart",
        amount=-100.0,
        currency="NZD",
        category="Groceries",
        raw_hash="cf-receipt-mix-nov17",
    )
    # 7. Recurring monthly + one-off + Annualized Dental
    rec_monthly = Transaction(
        account_id=checking.id,
        transaction_date=date(2026, 11, 5),
        merchant="Cloud Storage",
        amount=-52.0,
        currency="NZD",
        category="Software & AI",
        recurrence="monthly",
        raw_hash="cf-rec-mon-nov05",
    )
    one_off = Transaction(
        account_id=checking.id,
        transaction_date=date(2026, 11, 17),
        merchant="Laptop Upgrade",
        amount=-1200.0,
        currency="NZD",
        category="Tech",
        recurrence="one-off",
        raw_hash="cf-oneoff-nov17",
    )
    dental_raw = Transaction(
        account_id=checking.id,
        transaction_date=date(2026, 10, 15),
        merchant="South Island Dental",
        amount=-240.0,
        currency="NZD",
        category="Dental",
        raw_hash="cf-dental-oct15",
    )

    memory_db.add_all(
        [
            salary,
            mixed_neg,
            mixed_pos,
            partial_sgd,
            pure_repay,
            lent_out,
            borrowed_out,
            transfer_parent,
            mixed_receipt_tx,
            rec_monthly,
            one_off,
            dental_raw,
        ]
    )
    memory_db.flush()

    receipt = Receipt(
        transaction_id=mixed_receipt_tx.id,
        merchant="North Shore Mart",
        receipt_date=date(2026, 11, 17),
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
                item_name="Fresh Veg",
                unit_price=60.0,
                total_price=60.0,
                category="Groceries",
            ),
            ReceiptItem(
                receipt_id=receipt.id,
                item_name="Cash Withdrawal",
                unit_price=40.0,
                total_price=40.0,
                category="Transfers",
            ),
        ]
    )
    memory_db.commit()

    # Parameter validation
    with pytest.raises(InvalidWindowError, match="flow must be"):
        project_cashflow_transactions(memory_db, ReportingWindow(period="month"), flow="net")  # type: ignore[arg-type]
    with pytest.raises(InvalidWindowError, match="offset must be"):
        project_cashflow_transactions(memory_db, ReportingWindow(period="month"), flow="inflow", offset=-1)

    for period in ("week", "last_week", "month", "last_month", "annual", "all"):
        win = ReportingWindow(period=period)
        wp = project_window(memory_db, win, group_by="leaf", reference_date=ref)

        inflow_res = project_cashflow_transactions(
            memory_db, win, flow="inflow", include_amortized=True, limit=500, reference_date=ref
        )
        inflow_sum = round(sum(_effective_cashflow_amount_nzd(it) for it in inflow_res.items), 2)
        assert inflow_sum == wp.total_income, f"Inflow mismatch on period={period}"
        assert all(not it.excluded_from_period_budget for it in inflow_res.items)

        outflow_res = project_cashflow_transactions(
            memory_db, win, flow="outflow", include_amortized=True, limit=500, reference_date=ref
        )
        actual_sum = sum(
            _effective_cashflow_amount_nzd(it)
            for it in outflow_res.items
            if not it.is_amortized_commitment and not it.excluded_from_period_budget
        )
        amort_sum = sum(
            float(it.amortized_period_amount_nzd or 0.0)
            for it in outflow_res.items
            if it.is_amortized_commitment
        )
        # On weekly/monthly windows, Dental 365-day smoothing (240 / divisor) lives in breakdown categories
        annual_cats_smoothed = 0.0
        if period in ("week", "last_week"):
            annual_cats_smoothed = round(240.0 / 52.0, 2)
        elif period in ("month", "last_month"):
            annual_cats_smoothed = round(240.0 / 12.0, 2)
        assert round(actual_sum + amort_sum + annual_cats_smoothed, 2) == wp.total_spend, (
            f"Outflow mismatch on period={period}"
        )

    # Custom date window parity
    custom_win = ReportingWindow(start_date=date(2026, 11, 1), end_date=date(2026, 11, 18))
    wp_custom = project_window(memory_db, custom_win, group_by="leaf", reference_date=ref)
    cf_custom_in = project_cashflow_transactions(
        memory_db, custom_win, flow="inflow", limit=500, reference_date=ref
    )
    cf_custom_out = project_cashflow_transactions(
        memory_db, custom_win, flow="outflow", include_amortized=False, limit=500, reference_date=ref
    )
    assert round(sum(_effective_cashflow_amount_nzd(it) for it in cf_custom_in.items), 2) == wp_custom.total_income
    assert round(sum(_effective_cashflow_amount_nzd(it) for it in cf_custom_out.items), 2) == wp_custom.total_spend


def _seed_loan(db: Session, name: str, loan_type: str) -> Loan:
    """Create a synthetic loan fixture for projection tests."""
    loan = Loan(
        name=name, lender="Sample Lender", loan_type=loan_type,
        principal_amount=1000.0, annual_interest_rate=0.0,
        term_months=(0 if loan_type == "lent" else 12),
        monthly_payment=(0.0 if loan_type == "lent" else 100.0),
        currency="NZD", start_date=date(2026, 1, 1), is_active=True,
    )
    db.add(loan)
    db.flush()
    return loan


def _seed_detached_snapshot_fixtures(db: Session) -> dict[str, int]:
    """Seed linked transfer, dividend counterpart, split loan, and payslip fixtures."""
    checking = _seed_account(db, name="Checking 1001", currency="NZD")
    savings = _seed_account(db, name="Savings 2002", currency="NZD")
    lent_loan = _seed_loan(db, "Friend Advance", "lent")
    tx_out = Transaction(
        account_id=checking.id, transaction_date=date(2026, 11, 10),
        merchant="Sweep Out", amount=-250.0, currency="NZD", category="Transfers", raw_hash="det-out",
    )
    tx_in = Transaction(
        account_id=savings.id, transaction_date=date(2026, 11, 10),
        merchant="Sweep In", amount=250.0, currency="NZD", category="Transfers", raw_hash="det-in",
    )
    dividend = Transaction(
        account_id=checking.id, counterpart_account_id=savings.id, transaction_date=date(2026, 11, 12),
        merchant="Fund Dividend", amount=120.0, currency="NZD", category="Income", raw_hash="det-div",
    )
    split_tx = Transaction(
        account_id=checking.id, transaction_date=date(2026, 11, 14),
        merchant="Shared Errand", amount=-90.0, currency="NZD", category="General", raw_hash="det-split",
    )
    split_tx.splits = [
        TransactionSplit(amount=-50.0, category="Debt & Loans", loan_id=lent_loan.id, tags='["loan"]'),
        TransactionSplit(amount=-40.0, category="Groceries", tags='["food"]'),
    ]
    db.add_all([tx_out, tx_in, dividend, split_tx])
    db.flush()
    tx_in.linked_transaction_id = tx_out.id
    slip = Receipt(
        transaction_id=dividend.id, document_type="payslip", receipt_date=date(2026, 11, 12),
        merchant="Fund Dividend", total_amount=120.0, currency="NZD", status="matched",
    )
    db.add(slip)
    db.commit()
    return {"tx_in_id": tx_in.id, "dividend_id": dividend.id, "split_id": split_tx.id, "slip_id": slip.id}


def test_project_transactions_detached_frozen_snapshot_after_session_close(memory_db: Session) -> None:
    """ProjectedTransaction and ProjectedSplit stay readable and immutable after db.close()."""
    ids = _seed_detached_snapshot_fixtures(memory_db)
    ref = date(2026, 11, 18)
    ledger = project_transactions(
        memory_db, TransactionQuery(window=ReportingWindow(period="month"), limit=50), reference_date=ref
    )
    inflows = project_transactions(
        memory_db, TransactionQuery(window=ReportingWindow(period="month"), flow="inflow"), reference_date=ref
    )
    memory_db.close()

    assert isinstance(ledger, TransactionProjection)
    by_id = {it.id: it for it in ledger.items}
    assert all(is_dataclass(it) and not hasattr(it, "transaction") for it in ledger.items)
    assert by_id[ids["tx_in_id"]].linked_account_name == "Checking 1001"
    assert by_id[ids["tx_in_id"]].linked_amount == -250.0
    assert by_id[ids["dividend_id"]].counterpart_account_name == "Savings 2002"
    assert by_id[ids["dividend_id"]].receipt_id == ids["slip_id"]
    assert by_id[ids["dividend_id"]].receipt_document_type == "payslip"
    split_row = by_id[ids["split_id"]]
    assert split_row.is_split is True and isinstance(split_row.splits[0], ProjectedSplit)
    assert split_row.splits[0].loan_name == "Friend Advance"
    assert split_row.splits[0].loan_type == "lent"
    assert inflows.items[0].account_name == "Checking 1001"
    with pytest.raises(FrozenInstanceError):
        split_row.amount = 0.0  # type: ignore[misc]


def _seed_three_level_hierarchy_data(db: Session, acct_id: int) -> dict[str, int]:
    """Seed Food -> Dining -> Fine Dining 3-level hierarchy and matching ledger rows."""
    dining = db.scalar(select(Category).where(Category.name == "Dining"))
    assert dining is not None
    fine = Category(name="Fine Dining", parent_id=dining.id, is_active=True, sort_order=15)
    db.add(fine)
    db.flush()
    txs = {
        "food": Transaction(
            account_id=acct_id, transaction_date=date(2026, 11, 17),
            merchant="Food Hall", amount=-25.0, currency="NZD", category="Food", raw_hash="h-food",
        ),
        "dining": Transaction(
            account_id=acct_id, transaction_date=date(2026, 11, 17),
            merchant="Bistro", amount=-40.0, currency="NZD", category="Dining", raw_hash="h-dining",
        ),
        "fine": Transaction(
            account_id=acct_id, transaction_date=date(2026, 11, 17),
            merchant="Degustation", amount=-150.0, currency="NZD", category="Fine Dining", raw_hash="h-fine",
        ),
        "groc": Transaction(
            account_id=acct_id, transaction_date=date(2026, 11, 17),
            merchant="Market", amount=-60.0, currency="NZD", category="Groceries", raw_hash="h-groc",
        ),
        "split": Transaction(
            account_id=acct_id, transaction_date=date(2026, 11, 17),
            merchant="Tasting + Wine", amount=-80.0, currency="NZD", category="General", raw_hash="h-split",
        ),
        "rec_fine": Transaction(
            account_id=acct_id, transaction_date=date(2026, 11, 1),
            merchant="Supper Club Monthly", amount=-104.0, currency="NZD",
            category="Fine Dining", recurrence="monthly", raw_hash="h-rec-fine",
        ),
    }
    txs["split"].splits = [
        TransactionSplit(amount=-50.0, category="Fine Dining"),
        TransactionSplit(amount=-30.0, category="Alcohol"),
    ]
    db.add_all(txs.values())
    db.commit()
    return {k: v.id for k, v in txs.items()}


def test_include_subclasses_root_and_three_level_child_category(memory_db: Session) -> None:
    """Fix 1: include_subclasses=True traverses full subtree for both roots and child categories."""
    acct = _seed_account(memory_db)
    ids = _seed_three_level_hierarchy_data(memory_db, acct.id)
    ref = date(2026, 11, 18)

    for flow_mode in (None, "outflow"):
        child_sub = project_transactions(
            memory_db,
            TransactionQuery(
                window=ReportingWindow(period="week"), category="Dining",
                include_subclasses=True, include_amortized=True, flow=flow_mode,
            ),
            reference_date=ref,
        )
        assert {it.id for it in child_sub.items} == {ids["dining"], ids["fine"], ids["split"], ids["rec_fine"]}
        amort = [it for it in child_sub.items if it.is_amortized_commitment]
        assert len(amort) == 1 and amort[0].id == ids["rec_fine"]

        exact_dining = project_transactions(
            memory_db,
            TransactionQuery(
                window=ReportingWindow(period="week"), category="Dining",
                include_subclasses=False, flow=flow_mode,
            ),
            reference_date=ref,
        )
        assert {it.id for it in exact_dining.items} == {ids["dining"]}

    root_sub = project_transactions(
        memory_db,
        TransactionQuery(window=ReportingWindow(period="week"), category="Food", include_subclasses=True),
        reference_date=ref,
    )
    assert {it.id for it in root_sub.items} == {
        ids["food"], ids["dining"], ids["fine"], ids["groc"], ids["split"]
    }


def _seed_exclusion_fixtures(db: Session, acct_id: int) -> dict[str, int]:
    """Seed Tech, Annualized Dental, lent/borrowed loans, and split rows for budget exclusion tests."""
    lent_loan = _seed_loan(db, "Friend Loan", "lent")
    borrowed_loan = _seed_loan(db, "Auto Finance", "borrowed")
    db.add(BudgetRule(category="Dental", weekly_limit=15.0, monthly_limit=65.0, budget_period="annual", is_active=True))
    rows = {
        "tech_var": Transaction(
            account_id=acct_id, transaction_date=date(2026, 11, 16),
            merchant="USB Hub", amount=-50.0, currency="NZD", category="Tech", raw_hash="ex-tv",
        ),
        "tech_oneoff": Transaction(
            account_id=acct_id, transaction_date=date(2026, 11, 16),
            merchant="Laptop", amount=-1200.0, currency="NZD", category="Tech",
            recurrence="one-off", raw_hash="ex-to",
        ),
        "tech_refund": Transaction(
            account_id=acct_id, transaction_date=date(2026, 11, 16),
            merchant="Hub Refund", amount=15.0, currency="NZD", category="Tech", raw_hash="ex-tr",
        ),
        "dental": Transaction(
            account_id=acct_id, transaction_date=date(2026, 11, 15),
            merchant="City Dental", amount=-600.0, currency="NZD", category="Dental", raw_hash="ex-den",
        ),
        "lent": Transaction(
            account_id=acct_id, transaction_date=date(2026, 11, 15),
            merchant="Advance", amount=-400.0, currency="NZD", category="Debt & Loans",
            loan_id=lent_loan.id, raw_hash="ex-lent",
        ),
        "borrowed": Transaction(
            account_id=acct_id, transaction_date=date(2026, 11, 15),
            merchant="Auto Payment", amount=-250.0, currency="NZD", category="Debt & Loans",
            loan_id=borrowed_loan.id, raw_hash="ex-borr",
        ),
        "income": Transaction(
            account_id=acct_id, transaction_date=date(2026, 11, 15),
            merchant="Payroll", amount=3000.0, currency="NZD", category="Income", raw_hash="ex-inc",
        ),
    }
    db.add_all(rows.values())
    db.commit()
    return {k: v.id for k, v in rows.items()}


def test_excluded_from_period_budget_ledger_lent_annual_one_off_refunds(memory_db: Session) -> None:
    """Fix 2: flow=None with include_amortized flags lent, Annualized, one-off, and positive refunds."""
    acct = _seed_account(memory_db)
    ids = _seed_exclusion_fixtures(memory_db, acct.id)
    ref = date(2026, 11, 18)

    mo_all = project_transactions(
        memory_db, TransactionQuery(window=ReportingWindow(period="month"), include_amortized=True),
        reference_date=ref,
    )
    excl_mo = {it.id: it.excluded_from_period_budget for it in mo_all.items}
    assert excl_mo[ids["tech_var"]] is False
    assert excl_mo[ids["tech_oneoff"]] is True
    assert excl_mo[ids["tech_refund"]] is True
    assert excl_mo[ids["dental"]] is True
    assert excl_mo[ids["lent"]] is True
    assert excl_mo[ids["borrowed"]] is False
    assert excl_mo[ids["income"]] is False

    all_res = project_transactions(
        memory_db, TransactionQuery(window=ReportingWindow(period="all"), include_amortized=True),
        reference_date=ref,
    )
    excl_all = {it.id: it.excluded_from_period_budget for it in all_res.items}
    assert excl_all[ids["lent"]] is True
    assert excl_all[ids["dental"]] is False

    no_amort = project_transactions(
        memory_db, TransactionQuery(window=ReportingWindow(period="month"), include_amortized=False),
        reference_date=ref,
    )
    assert all(not it.excluded_from_period_budget for it in no_amort.items)


def _seed_tagged_window_fixtures(db: Session, acct_id: int) -> dict[str, int]:
    """Seed tagged regular, one-off, annual, transfer, receipt, and lent transactions."""
    lent_loan = _seed_loan(db, "Friend Trip", "lent")
    db.add(BudgetRule(category="Dental", weekly_limit=15.0, monthly_limit=65.0, budget_period="annual", is_active=True))
    t_reg = Transaction(
        account_id=acct_id, transaction_date=date(2026, 11, 10),
        merchant="Flights", amount=-500.0, currency="NZD", category="Travel",
        tags='["trip"]', raw_hash="tag-reg",
    )
    t_oneoff = Transaction(
        account_id=acct_id, transaction_date=date(2026, 11, 11),
        merchant="Camera", amount=-1200.0, currency="NZD", category="Tech",
        recurrence="one-off", tags='["trip"]', raw_hash="tag-oneoff",
    )
    t_dental = Transaction(
        account_id=acct_id, transaction_date=date(2026, 11, 12),
        merchant="Travel Clinic", amount=-300.0, currency="NZD", category="Dental",
        tags='["trip"]', raw_hash="tag-dental",
    )
    t_xfer = Transaction(
        account_id=acct_id, transaction_date=date(2026, 11, 13),
        merchant="FX Sweep", amount=-400.0, currency="NZD", category="Transfers",
        tags='["trip"]', raw_hash="tag-xfer",
    )
    t_lent = Transaction(
        account_id=acct_id, transaction_date=date(2026, 11, 14),
        merchant="Lend Friend", amount=-250.0, currency="NZD", category="Debt & Loans",
        loan_id=lent_loan.id, tags='["trip"]', raw_hash="tag-lent",
    )
    t_split = Transaction(
        account_id=acct_id, transaction_date=date(2026, 11, 15),
        merchant="Split Meal", amount=-100.0, currency="NZD", category="General",
        tags='["trip"]', raw_hash="tag-split",
    )
    t_split.splits = [
        TransactionSplit(amount=-60.0, category="Dining"),
        TransactionSplit(amount=-40.0, category="Transfers"),
    ]
    t_rec = Transaction(
        account_id=acct_id, transaction_date=date(2026, 11, 16),
        merchant="Supermart", amount=-100.0, currency="NZD", category="Groceries",
        tags='["trip"]', raw_hash="tag-rec",
    )
    db.add_all([t_reg, t_oneoff, t_dental, t_xfer, t_lent, t_split, t_rec])
    db.flush()
    rec = Receipt(
        transaction_id=t_rec.id, merchant="Supermart", receipt_date=date(2026, 11, 16),
        total_amount=100.0, currency="NZD", status="matched",
    )
    db.add(rec)
    db.flush()
    db.add_all([
        ReceiptItem(receipt_id=rec.id, item_name="Snacks", unit_price=60.0, total_price=60.0, category="Groceries"),
        ReceiptItem(receipt_id=rec.id, item_name="Cash Out", unit_price=40.0, total_price=40.0, category="Transfers"),
    ])
    db.commit()
    return {"xfer_id": t_xfer.id, "lent_id": t_lent.id}


def test_project_window_tags_excludes_transfers_and_lent_while_keeping_one_off_and_annual(
    memory_db: Session,
) -> None:
    """Fix 3: project_window.tags excludes Transfers/lent; ?tag= ledger still returns tagged Transfers."""
    acct = _seed_account(memory_db)
    ids = _seed_tagged_window_fixtures(memory_db, acct.id)
    ref = date(2026, 11, 18)

    wp = project_window(memory_db, ReportingWindow(period="month"), reference_date=ref)
    assert wp.total_tagged_spend == 2120.0  # 500 + 1200 + 300 + 60 + 60
    assert len(wp.tags) == 1
    trip = wp.tags[0]
    assert trip.tag == "trip"
    assert trip.total_spend == 2120.0
    assert trip.transaction_count == 5
    cat_names = {c.category for c in trip.categories}
    assert cat_names == {"Travel", "Tech", "Dental", "Dining", "Groceries"}

    ledger_tag = project_transactions(
        memory_db, TransactionQuery(window=ReportingWindow(period="month"), tag="trip"), reference_date=ref
    )
    assert ids["xfer_id"] in {it.id for it in ledger_tag.items}
    assert ids["lent_id"] in {it.id for it in ledger_tag.items}


def test_http_summary_categories_summary_tags_and_ledger_contracts(
    api_client: TestClient,
    memory_db: Session,
) -> None:
    """Thin HTTP contract smoke tests for /summary/categories, /summary/tags, and /transactions."""
    today = date.today()
    sgd_acct = _seed_account(memory_db, name="SGD Cash", currency="SGD")
    nzd_acct = _seed_account(memory_db, name="NZD Checking", currency="NZD")
    lent_loan = _seed_loan(memory_db, "Travel Advance", "lent")
    memory_db.add_all([
        BudgetRule(category="Dental", weekly_limit=15.0, monthly_limit=65.0, budget_period="annual", is_active=True),
        BudgetRule(category="Utilities", weekly_limit=50.0, monthly_limit=216.67, budget_period="monthly", is_active=True),
    ])
    t_sgd = Transaction(
        account_id=sgd_acct.id, transaction_date=today, merchant="SGD Cafe", amount=-100.0,
        currency="SGD", exchange_rate=1.30, category="Dining", tags='["asia-trip"]', raw_hash="http-sgd",
    )
    t_lent = Transaction(
        account_id=nzd_acct.id, transaction_date=today, merchant="Lent Cash", amount=-300.0,
        currency="NZD", category="Debt & Loans", loan_id=lent_loan.id, tags='["asia-trip"]', raw_hash="http-lent",
    )
    t_xfer = Transaction(
        account_id=nzd_acct.id, transaction_date=today, merchant="FX Transfer", amount=-400.0,
        currency="NZD", category="Transfers", tags='["asia-trip"]', raw_hash="http-xfer",
    )
    t_oneoff = Transaction(
        account_id=nzd_acct.id, transaction_date=today, merchant="Gear", amount=-200.0,
        currency="NZD", category="Tech", recurrence="one-off", tags='["asia-trip"]', raw_hash="http-oneoff",
    )
    t_rec = Transaction(
        account_id=nzd_acct.id, transaction_date=today, merchant="Mart", amount=-100.0,
        currency="NZD", category="Groceries", tags='["asia-trip"]', raw_hash="http-mart",
    )
    memory_db.add_all([t_sgd, t_lent, t_xfer, t_oneoff, t_rec])
    memory_db.flush()
    rec = Receipt(
        transaction_id=t_rec.id, merchant="Mart", receipt_date=today, total_amount=100.0, currency="NZD", status="matched"
    )
    memory_db.add(rec)
    memory_db.flush()
    memory_db.add_all([
        ReceiptItem(receipt_id=rec.id, item_name="Food", unit_price=60.0, total_price=60.0, category="Groceries"),
        ReceiptItem(receipt_id=rec.id, item_name="ATM", unit_price=40.0, total_price=40.0, category="Transfers"),
    ])
    memory_db.commit()

    bad_cat = api_client.get("/transactions/summary/categories?start_date=2026-11-20&end_date=2026-11-10")
    assert bad_cat.status_code == 400

    cat_sum = api_client.get(f"/transactions/summary/categories?start_date={today.isoformat()}&end_date={today.isoformat()}")
    assert cat_sum.status_code == 200
    assert cat_sum.json() == [
        {"category": "Tech", "total_amount": -200.0, "count": 1},
        {"category": "Dining", "total_amount": -130.0, "count": 1},
        {"category": "Groceries", "total_amount": -60.0, "count": 1},
    ]

    bad_tag = api_client.get("/transactions/summary/tags?period=invalid")
    assert bad_tag.status_code == 400
    tag_sum = api_client.get("/transactions/summary/tags?period=month")
    assert tag_sum.status_code == 200
    tag_body = tag_sum.json()
    assert tag_body["total_tagged_spend"] == 390.0  # 130 Dining + 200 Tech + 60 Groceries
    assert tag_body["tags"][0]["transaction_count"] == 3

    ledger_tag = api_client.get("/transactions?tag=asia-trip&period=month&include_amortized=true")
    assert ledger_tag.status_code == 200
    by_id = {row["id"]: row for row in ledger_tag.json()}
    assert t_xfer.id in by_id
    assert by_id[t_lent.id]["excluded_from_period_budget"] is True
    assert by_id[t_oneoff.id]["excluded_from_period_budget"] is True
