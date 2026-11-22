"""FastAPI HTTP parity and edge-case tests for GET /transactions?flow=inflow|outflow."""

from collections.abc import Iterator
from datetime import date, timedelta

from fastapi.testclient import TestClient
import pytest
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
from app.services.fx_service import to_nzd_amount


@pytest.fixture()
def memory_db() -> Iterator[Session]:
    """Create an isolated SQLite :memory: session with default Plutus categories seeded."""
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
    """Provide a FastAPI TestClient wired to the isolated SQLite :memory: session."""

    def _override_get_db() -> Iterator[Session]:
        yield memory_db

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def _seed_account(db: Session, name: str, currency: str = "NZD") -> Account:
    acct = Account(
        name=name,
        institution="Test Bank",
        account_type="checking",
        tier="cash",
        currency=currency,
    )
    db.add(acct)
    db.commit()
    db.refresh(acct)
    return acct


def _tx_nzd_magnitude(row: dict) -> float:
    return round(
        to_nzd_amount(abs(float(row["amount"])), row["currency"], row.get("exchange_rate")),
        2,
    )


def test_transactions_flow_cors_validation_and_payslip_hydration(
    api_client: TestClient,
    memory_db: Session,
) -> None:
    """Verify CORS Expose-Headers, invalid flow/window errors, and payslip receipt hydration."""
    today = date.today()
    acct = _seed_account(memory_db, "Primary Checking")

    salary = Transaction(
        account_id=acct.id,
        transaction_date=today,
        merchant="Acme Corp Payroll",
        amount=4200.0,
        currency="NZD",
        category="Income",
        tags='["salary"]',
        raw_hash="flow-http-salary",
    )
    memory_db.add(salary)
    memory_db.flush()

    payslip = Receipt(
        transaction_id=salary.id,
        document_type="payslip",
        receipt_date=today,
        merchant="Acme Corp",
        total_amount=4200.0,
        currency="NZD",
        status="matched",
        image_mime_type="application/pdf",
        image_filename="payslip-1001.pdf",
    )
    memory_db.add(payslip)
    memory_db.commit()
    memory_db.refresh(payslip)

    # CORS exposes X-Total-Count header
    res = api_client.get(
        "/transactions?flow=inflow&period=month&include_amortized=true",
        headers={"Origin": "https://midas.example"},
    )
    assert res.status_code == 200
    assert res.headers["X-Total-Count"] == "1"
    expose_hdr = res.headers.get("Access-Control-Expose-Headers", "")
    assert "X-Total-Count" in expose_hdr

    rows = res.json()
    assert len(rows) == 1
    assert rows[0]["id"] == salary.id
    assert rows[0]["has_receipt"] is True
    assert rows[0]["receipt_id"] == payslip.id
    assert rows[0]["receipt_document_type"] == "payslip"
    assert rows[0]["excluded_from_period_budget"] is False
    assert rows[0]["is_amortized_commitment"] is False

    # Invalid flow -> HTTP 422
    bad_flow = api_client.get("/transactions?flow=net")
    assert bad_flow.status_code == 422

    # Invalid period / date bounds -> HTTP 400
    bad_period = api_client.get("/transactions?flow=outflow&period=quarterly")
    assert bad_period.status_code == 400
    assert "Invalid period" in bad_period.json()["detail"]

    bad_dates = api_client.get(
        "/transactions?flow=inflow&start_date=2026-11-20&end_date=2026-11-10"
    )
    assert bad_dates.status_code == 400
    assert "start_date must be on or before end_date" in bad_dates.json()["detail"]


def test_transactions_flow_http_parity_across_all_windows_and_ledger_edge_cases(
    api_client: TestClient,
    memory_db: Session,
) -> None:
    """Verify HTTP parity with GET /budgets/breakdown across all windows and edge cases."""
    today = date.today()
    this_monday = today - timedelta(days=today.weekday())
    last_week_day = this_monday - timedelta(days=3)
    prev_month_day = date(today.year, today.month, 1) - timedelta(days=5)

    checking = _seed_account(memory_db, "Checking NZD")
    sgd_acct = _seed_account(memory_db, "Travel SGD", currency="SGD")
    brokerage = _seed_account(memory_db, "Brokerage Equity", currency="SGD")

    lent_loan = Loan(
        name="Advance to Jordan",
        lender="Jordan Lee",
        loan_type="lent",
        principal_amount=3000.0,
        annual_interest_rate=0.0,
        term_months=0,
        monthly_payment=0.0,
        currency="NZD",
        start_date=prev_month_day,
        is_active=True,
    )
    borrowed_loan = Loan(
        name="Home Improvement Loan",
        lender="North Bank",
        loan_type="borrowed",
        principal_amount=5000.0,
        annual_interest_rate=6.0,
        term_months=24,
        monthly_payment=220.0,
        currency="NZD",
        start_date=prev_month_day,
        is_active=True,
    )
    memory_db.add_all([lent_loan, borrowed_loan])
    memory_db.flush()

    memory_db.add(
        BudgetRule(
            category="Dental",
            weekly_limit=15.0,
            monthly_limit=65.0,
            budget_period="annual",
            is_active=True,
        )
    )

    # 1. Mixed-sign splits: negative parent with positive Income split; positive parent with negative Travel split
    tx_neg_mixed = Transaction(
        account_id=checking.id,
        transaction_date=today,
        merchant="Cafe Shared Bill",
        amount=-40.0,
        currency="NZD",
        category="Dining",
        tags='["friends"]',
        raw_hash="http-neg-mixed",
    )
    tx_neg_mixed.splits = [
        TransactionSplit(amount=-70.0, category="Dining", tags='["lunch"]'),
        TransactionSplit(amount=30.0, category="Income", tags='["reimbursed"]', notes="Friend share"),
    ]

    tx_pos_mixed = Transaction(
        account_id=checking.id,
        transaction_date=last_week_day,
        merchant="Conference Stipend Net",
        amount=137.17,
        currency="NZD",
        category="General",
        raw_hash="http-pos-mixed",
    )
    tx_pos_mixed.splits = [
        TransactionSplit(amount=1000.0, category="Debt & Loans", loan_id=lent_loan.id),
        TransactionSplit(amount=-862.83, category="Travel", tags='["conf"]', notes="Flight"),
    ]

    # 2. Outflow split with lent loan + Transfers + Groceries
    tx_split_xfer_lent = Transaction(
        account_id=checking.id,
        transaction_date=today,
        merchant="Combined Errand",
        amount=-100.0,
        currency="NZD",
        category="General",
        raw_hash="http-split-xfer-lent",
    )
    tx_split_xfer_lent.splits = [
        TransactionSplit(amount=-50.0, category="Debt & Loans", loan_id=lent_loan.id),
        TransactionSplit(amount=-20.0, category="Transfers"),
        TransactionSplit(amount=-30.0, category="Groceries", tags='["snacks"]'),
    ]

    # 3. Partial lent repayment in SGD (150 * 1.30 = 195 NZD; 100 * 1.30 = 130 lent -> 65 NZD income)
    partial_repay = Transaction(
        account_id=sgd_acct.id,
        transaction_date=prev_month_day,
        merchant="Jordan Partial Repay",
        amount=150.0,
        currency="SGD",
        exchange_rate=1.30,
        category="Debt & Loans",
        loan_id=lent_loan.id,
        loan_payment_amount=100.0,
        raw_hash="http-partial-repay",
    )
    # Full lent repayment (excluded from inflow) & borrowed loan repayment (included in outflow)
    full_repay = Transaction(
        account_id=checking.id,
        transaction_date=today,
        merchant="Jordan Full Repay",
        amount=250.0,
        currency="NZD",
        category="Debt & Loans",
        loan_id=lent_loan.id,
        raw_hash="http-full-repay",
    )
    borrowed_out = Transaction(
        account_id=checking.id,
        transaction_date=today,
        merchant="Loan Installment",
        amount=-220.0,
        currency="NZD",
        category="Debt & Loans",
        loan_id=borrowed_loan.id,
        raw_hash="http-borrowed-out",
    )

    # 4. Unlinked dividend inflow with counterpart_account_id=brokerage.id
    dividend = Transaction(
        account_id=sgd_acct.id,
        counterpart_account_id=brokerage.id,
        transaction_date=today,
        merchant="Equity Dividend",
        amount=100.0,
        currency="SGD",
        exchange_rate=1.30,
        category="Income",
        raw_hash="http-dividend",
    )

    # 5. Transfers parent (always excluded) & mixed Transfers Receipt Line
    xfer_parent = Transaction(
        account_id=checking.id,
        transaction_date=today,
        merchant="Savings Sweep",
        amount=-300.0,
        currency="NZD",
        category="Transfers",
        raw_hash="http-xfer-parent",
    )
    receipt_mix_tx = Transaction(
        account_id=checking.id,
        transaction_date=today,
        merchant="North Shore Mart",
        amount=-100.0,
        currency="NZD",
        category="Groceries",
        raw_hash="http-receipt-mix",
    )

    # 6. Recurring monthly + one-off + Annualized Dental
    rec_split = Transaction(
        account_id=checking.id,
        transaction_date=today - timedelta(days=2),
        merchant="Cloud Bundle",
        amount=-130.0,
        currency="NZD",
        category="Utilities",
        recurrence="monthly",
        raw_hash="http-rec-split",
    )
    rec_split.splits = [
        TransactionSplit(amount=-78.0, category="Bills & Telco"),
        TransactionSplit(amount=-52.0, category="Software & AI"),
    ]
    one_off_tx = Transaction(
        account_id=checking.id,
        transaction_date=today,
        merchant="Workstation Desk",
        amount=-900.0,
        currency="NZD",
        category="Household",
        recurrence="one-off",
        raw_hash="http-oneoff",
    )
    dental_tx = Transaction(
        account_id=checking.id,
        transaction_date=prev_month_day,
        merchant="South Island Dental",
        amount=-600.0,
        currency="NZD",
        category="Dental",
        raw_hash="http-dental",
    )

    memory_db.add_all(
        [
            tx_neg_mixed,
            tx_pos_mixed,
            tx_split_xfer_lent,
            partial_repay,
            full_repay,
            borrowed_out,
            dividend,
            xfer_parent,
            receipt_mix_tx,
            rec_split,
            one_off_tx,
            dental_tx,
        ]
    )
    memory_db.flush()

    rec = Receipt(
        transaction_id=receipt_mix_tx.id,
        merchant="North Shore Mart",
        receipt_date=today,
        total_amount=100.0,
        currency="NZD",
        status="matched",
    )
    memory_db.add(rec)
    memory_db.flush()
    memory_db.add_all(
        [
            ReceiptItem(
                receipt_id=rec.id,
                item_name="Fresh Fruit",
                unit_price=60.0,
                total_price=60.0,
                category="Groceries",
            ),
            ReceiptItem(
                receipt_id=rec.id,
                item_name="EFTPOS Cash Out",
                unit_price=40.0,
                total_price=40.0,
                category="Transfers",
            ),
        ]
    )
    memory_db.commit()

    # Check exact parity across every Reporting Window
    for period in ("week", "last_week", "month", "last_month", "annual", "all"):
        bd = api_client.get(f"/budgets/breakdown?period={period}").json()

        inflow_rows = api_client.get(
            f"/transactions?flow=inflow&period={period}&limit=500"
        ).json()
        inflow_sum = round(sum(_tx_nzd_magnitude(r) for r in inflow_rows), 2)
        assert inflow_sum == bd["total_income"], f"Inflow parity failed for {period}"

        outflow_rows = api_client.get(
            f"/transactions?flow=outflow&period={period}&include_amortized=true&limit=500"
        ).json()
        actual_spend = sum(
            _tx_nzd_magnitude(r)
            for r in outflow_rows
            if not r["is_amortized_commitment"] and not r["excluded_from_period_budget"]
        )
        amort_spend = sum(
            float(r["amortized_period_amount_nzd"] or 0.0)
            for r in outflow_rows
            if r["is_amortized_commitment"]
        )
        annual_cats_equiv = 0.0
        if period in ("week", "last_week"):
            annual_cats_equiv = round(600.0 / 52.0, 2)
        elif period in ("month", "last_month"):
            annual_cats_equiv = round(600.0 / 12.0, 2)
        assert round(actual_spend + amort_spend + annual_cats_equiv, 2) == bd["total_spend"], (
            f"Outflow parity failed for {period}"
        )

    # Custom date window parity
    custom_start = prev_month_day.isoformat()
    custom_end = today.isoformat()
    bd_custom = api_client.get(
        f"/budgets/breakdown?start_date={custom_start}&end_date={custom_end}"
    ).json()
    custom_in = api_client.get(
        f"/transactions?flow=inflow&start_date={custom_start}&end_date={custom_end}&limit=500"
    ).json()
    custom_out_only_active = api_client.get(
        f"/transactions?flow=outflow&start_date={custom_start}&end_date={custom_end}&include_amortized=false&limit=500"
    ).json()
    assert round(sum(_tx_nzd_magnitude(r) for r in custom_in), 2) == bd_custom["total_income"]
    assert (
        round(sum(_tx_nzd_magnitude(r) for r in custom_out_only_active), 2)
        == bd_custom["total_spend"]
    )
    # On custom window with include_amortized=true, ONLY lent outflows are excluded_from_period_budget=True
    custom_out_all = api_client.get(
        f"/transactions?flow=outflow&start_date={custom_start}&end_date={custom_end}&include_amortized=true&limit=500"
    ).json()
    excluded_custom = [r for r in custom_out_all if r["excluded_from_period_budget"]]
    assert len(excluded_custom) == 1
    assert excluded_custom[0]["id"] == tx_split_xfer_lent.id
    assert excluded_custom[0]["amount"] == -50.0

    # Mixed-sign split details & partial receipt split details in flow=inflow and flow=outflow
    in_all = api_client.get("/transactions?flow=inflow&period=all").json()
    in_by_id = {r["id"]: r for r in in_all}
    assert tx_neg_mixed.id in in_by_id
    assert in_by_id[tx_neg_mixed.id]["amount"] == 30.0
    assert in_by_id[tx_neg_mixed.id]["category"] == "Income"
    assert in_by_id[tx_neg_mixed.id]["is_split"] is True
    assert in_by_id[tx_neg_mixed.id]["tags"] == ["friends", "reimbursed"]
    assert in_by_id[partial_repay.id]["amount"] == 50.0
    assert in_by_id[partial_repay.id]["loan_type"] == "lent"
    assert full_repay.id not in in_by_id
    assert xfer_parent.id not in in_by_id

    out_all = api_client.get(
        "/transactions?flow=outflow&period=all&include_amortized=false"
    ).json()
    out_by_id = {r["id"]: r for r in out_all}
    assert out_by_id[tx_pos_mixed.id]["amount"] == -862.83
    assert out_by_id[tx_pos_mixed.id]["category"] == "Travel"
    assert out_by_id[receipt_mix_tx.id]["amount"] == -60.0
    assert out_by_id[receipt_mix_tx.id]["is_receipt_split"] is True
    assert out_by_id[borrowed_out.id]["loan_type"] == "borrowed"
    assert out_by_id[tx_split_xfer_lent.id]["amount"] == -30.0

    # Combined flow + category (exact and Root Category subclasses) + account_id (unlinked counterpart)
    food_out = api_client.get(
        "/transactions?flow=outflow&period=all&category=Food&include_subclasses=true"
    ).json()
    assert round(sum(_tx_nzd_magnitude(r) for r in food_out), 2) == 160.0  # 70 Dining + 30 Groceries + 60 Receipt

    xfer_filter = api_client.get("/transactions?flow=outflow&period=all&category=Transfers")
    assert xfer_filter.status_code == 200
    assert xfer_filter.json() == []
    assert xfer_filter.headers["X-Total-Count"] == "0"

    brokerage_in = api_client.get(
        f"/transactions?flow=inflow&period=all&account_id={brokerage.id}"
    ).json()
    assert len(brokerage_in) == 1
    assert brokerage_in[0]["id"] == dividend.id

    # Verify excluded_from_period_budget=True on one-off, lent, Annualized Category, and recurring rows
    wk_out_incl = api_client.get(
        "/transactions?flow=outflow&period=week&include_amortized=true&limit=500"
    ).json()
    wk_excl_ids = {r["id"] for r in wk_out_incl if r["excluded_from_period_budget"]}
    assert one_off_tx.id in wk_excl_ids
    assert tx_split_xfer_lent.id in wk_excl_ids

    wk_out_no_incl = api_client.get(
        "/transactions?flow=outflow&period=week&include_amortized=false&limit=500"
    ).json()
    assert all(not r["excluded_from_period_budget"] for r in wk_out_no_incl)
    assert all(not r["is_amortized_commitment"] for r in wk_out_no_incl)
    assert one_off_tx.id not in {r["id"] for r in wk_out_no_incl}

    last_mo_incl = api_client.get(
        "/transactions?flow=outflow&period=last_month&include_amortized=true&limit=500"
    ).json()
    dental_rows = [r for r in last_mo_incl if r["id"] == dental_tx.id]
    assert len(dental_rows) == 1
    assert dental_rows[0]["excluded_from_period_budget"] is True
    assert dental_rows[0]["is_amortized_commitment"] is False


def test_transactions_flow_pagination_and_no_duplicate_commitments(
    api_client: TestClient,
    memory_db: Session,
) -> None:
    """Verify offset/limit pagination, X-Total-Count, and amortized commitments only on offset=0."""
    today = date.today()
    acct = _seed_account(memory_db, "Daily Checking")

    for idx in range(5):
        memory_db.add(
            Transaction(
                account_id=acct.id,
                transaction_date=today - timedelta(days=idx),
                merchant=f"Grocery Store {idx}",
                amount=-10.0 * (idx + 1),
                currency="NZD",
                category="Groceries",
                raw_hash=f"page-groc-{idx}",
            )
        )
    # Active monthly commitment (amortized in period=week)
    memory_db.add(
        Transaction(
            account_id=acct.id,
            transaction_date=today - timedelta(days=10),
            merchant="Monthly Fiber",
            amount=-104.0,
            currency="NZD",
            category="Bills & Telco",
            recurrence="monthly",
            raw_hash="page-rec-fiber",
        )
    )
    memory_db.commit()

    # In period=all (no commitments), 6 regular rows exist; paginate 2 at a time
    p1 = api_client.get("/transactions?flow=outflow&period=all&offset=0&limit=2")
    assert p1.status_code == 200
    assert p1.headers["X-Total-Count"] == "6"
    assert len(p1.json()) == 2

    p2 = api_client.get("/transactions?flow=outflow&period=all&offset=2&limit=2")
    assert p2.headers["X-Total-Count"] == "6"
    assert len(p2.json()) == 2

    p3 = api_client.get("/transactions?flow=outflow&period=all&offset=4&limit=5")
    assert p3.headers["X-Total-Count"] == "6"
    assert len(p3.json()) == 2

    all_ids = [r["id"] for r in p1.json() + p2.json() + p3.json()]
    assert len(set(all_ids)) == 6

    # In period=week with include_amortized=true, amortized commitment appears ONLY when offset=0
    wk_p1 = api_client.get(
        "/transactions?flow=outflow&period=week&include_amortized=true&offset=0&limit=1"
    )
    assert wk_p1.status_code == 200
    wk_total = int(wk_p1.headers["X-Total-Count"])
    assert wk_total >= 1
    wk_p1_items = wk_p1.json()
    assert len([r for r in wk_p1_items if not r["is_amortized_commitment"]]) == 1
    wk_p1_amort = [r for r in wk_p1_items if r["is_amortized_commitment"]]
    assert len(wk_p1_amort) == 1
    assert wk_p1_amort[0]["amortized_cadence"] == "week"
    assert wk_p1_amort[0]["amortized_period_amount_nzd"] == 24.0

    wk_p2 = api_client.get(
        "/transactions?flow=outflow&period=week&include_amortized=true&offset=1&limit=10"
    )
    assert wk_p2.status_code == 200
    assert wk_p2.headers["X-Total-Count"] == str(wk_total)
    assert all(not r["is_amortized_commitment"] for r in wk_p2.json())

    # Legacy mode (flow omitted) stays intact and exposes X-Total-Count + offset
    legacy = api_client.get("/transactions?offset=0&limit=2")
    assert legacy.status_code == 200
    assert legacy.headers["X-Total-Count"] == "6"
    assert len(legacy.json()) == 2
