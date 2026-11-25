"""Unit tests for detached loan snapshots, decomposed amortization, and non-NZD FX."""

from dataclasses import FrozenInstanceError
from datetime import date
import itertools
from typing import Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Account, ExchangeRate, Loan, Transaction, TransactionSplit
from app.services.loan_service import (
    LoanFxConversionError,
    LoanPaymentSnapshot,
    LoanPortfolioSummary,
    LoanStatusSnapshot,
    add_months,
    compute_loan_status,
    get_loan_portfolio_summary,
    get_loan_status,
    load_loan_with_transactions,
    summarize_loans,
)

_RAW_HASH_SEQ = itertools.count(1)


class _FakeFxRateFetcher:
    """Deterministic fake FxRateFetcher recording all lookup calls."""

    def __init__(self, rates: Optional[dict[tuple[date, str, str], tuple[float, str]]] = None) -> None:
        self.rates = rates or {}
        self.calls: list[tuple[date, str, str]] = []

    def fetch_rate(
        self,
        *,
        rate_date: date,
        from_currency: str,
        to_currency: str,
    ) -> Optional[tuple[float, str]]:
        key = (rate_date, from_currency.upper(), to_currency.upper())
        self.calls.append(key)
        return self.rates.get(key)


def _create_test_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    return engine, TestingSessionLocal


def _seed_account(db, *, name: str = "Main Checking", currency: str = "NZD") -> Account:
    account = Account(
        name=name,
        institution="Test Bank",
        account_type="checking",
        tier="cash",
        currency=currency,
    )
    db.add(account)
    db.flush()
    return account


def _make_tx(**kwargs) -> Transaction:
    kwargs.setdefault("raw_hash", f"test-hash-{next(_RAW_HASH_SEQ)}")
    return Transaction(**kwargs)


def _seed_multi_tx_lent_loan(db, acc_id: int) -> int:
    loan = Loan(
        name="Equipment Loan",
        lender="Alpha Finance",
        loan_type="lent",
        principal_amount=1000.0,
        annual_interest_rate=0.0,
        term_months=0,
        monthly_payment=0.0,
        start_date=date(2025, 1, 1),
        currency="NZD",
        is_active=True,
    )
    db.add(loan)
    db.flush()

    split_parent_1 = _make_tx(
        account_id=acc_id, transaction_date=date(2025, 3, 1), amount=250.0,
        currency="NZD", merchant="Alex Tan", category="General",
    )
    split_parent_2 = _make_tx(
        account_id=acc_id, transaction_date=date(2025, 4, 1), amount=150.0,
        currency="NZD", merchant="Alex Tan", category="General",
    )
    db.add_all([
        _make_tx(
            account_id=acc_id, transaction_date=date(2025, 1, 1), amount=-1000.0,
            currency="NZD", merchant="Alex Tan", category="Debt & Loans", loan_id=loan.id,
        ),
        _make_tx(
            account_id=acc_id, transaction_date=date(2025, 2, 1), amount=300.0,
            currency="NZD", merchant="Alex Tan", category="Debt & Loans", loan_id=loan.id,
        ),
        split_parent_1,
        split_parent_2,
    ])
    db.flush()
    db.add_all([
        TransactionSplit(transaction_id=split_parent_1.id, amount=200.0, category="Debt & Loans", loan_id=loan.id, notes="Split repay 1"),
        TransactionSplit(transaction_id=split_parent_1.id, amount=50.0, category="Income"),
        TransactionSplit(transaction_id=split_parent_2.id, amount=100.0, category="Debt & Loans", loan_id=loan.id, notes="Split repay 2"),
        TransactionSplit(transaction_id=split_parent_2.id, amount=50.0, category="Income"),
    ])
    db.commit()
    return loan.id


def test_detached_frozen_snapshots_survive_session_close_and_multi_tx_load():
    engine, SessionLocal = _create_test_session()
    with SessionLocal() as db:
        nzd_acc = _seed_account(db, name="Alpha Checking", currency="NZD")
        loan_id = _seed_multi_tx_lent_loan(db, nzd_acc.id)

    with SessionLocal() as db:
        loaded = load_loan_with_transactions(db, loan_id)
        assert loaded is not None
        status = get_loan_status(db, loan_id, as_of=date(2025, 5, 1))
        summary = get_loan_portfolio_summary(db, as_of=date(2025, 5, 1))
        assert get_loan_status(db, 999999, as_of=date(2025, 5, 1)) is None

    engine.dispose()

    assert isinstance(status, LoanStatusSnapshot)
    assert isinstance(summary, LoanPortfolioSummary)
    assert isinstance(summary.loans, tuple)
    assert isinstance(status.linked_payments, tuple)
    assert isinstance(status.disbursements, tuple)
    assert all(isinstance(p, LoanPaymentSnapshot) for p in status.linked_payments)
    assert status.remaining_principal == 400.0
    assert status.payments_made == 3
    assert status.linked_payments[0].account_name == "Alpha Checking"
    assert summary.total_receivables == 400.0

    with pytest.raises(FrozenInstanceError):
        status.remaining_principal = 0.0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        summary.total_liabilities = 0.0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        status.linked_payments[0].applied_amount = 0.0  # type: ignore[misc]


def test_add_months_clamps_month_end_and_leap_years():
    assert add_months(date(2025, 1, 31), 1) == date(2025, 2, 28)
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months(date(2025, 1, 31), 12) == date(2026, 1, 31)
    assert add_months(date(2025, 3, 31), 1) == date(2025, 4, 30)


def test_borrowed_interest_early_payoff_due_states_and_inactive_exclusion():
    engine, SessionLocal = _create_test_session()
    with SessionLocal() as db:
        acc = _seed_account(db, name="Checking 1001", currency="NZD")
        borrowed = Loan(
            name="Vehicle Loan", lender="Beta Finance", loan_type="borrowed",
            principal_amount=1000.0, annual_interest_rate=0.12, term_months=12,
            monthly_payment=100.0, start_date=date(2025, 1, 15),
            asset_value=1500.0, currency="NZD", is_active=True,
        )
        inactive = Loan(
            name="Closed Loan", lender="Beta Finance", loan_type="borrowed",
            principal_amount=5000.0, annual_interest_rate=0.05, term_months=12,
            monthly_payment=450.0, start_date=date(2024, 1, 1),
            currency="NZD", is_active=False,
        )
        db.add_all([borrowed, inactive])
        db.flush()
        db.add(_make_tx(
            account_id=acc.id, transaction_date=date(2025, 1, 15), amount=-110.0,
            currency="NZD", merchant="Beta Finance", category="Debt & Loans", loan_id=borrowed.id,
        ))
        db.commit()

        before_due = get_loan_status(db, borrowed.id, as_of=date(2025, 2, 14))
        assert before_due is not None and before_due.interest_paid == 10.0
        assert before_due.remaining_principal == 900.0
        assert before_due.next_payment_date == date(2025, 2, 15)
        assert before_due.is_payment_due is False

        on_due = get_loan_status(db, borrowed.id, as_of=date(2025, 2, 15))
        assert on_due is not None and on_due.is_payment_due is True

        # Payoff leaving <= 0.01 (900 + 9 interest = 909; paying 908.995 leaves 0.005 -> clamped 0.0)
        db.add(_make_tx(
            account_id=acc.id, transaction_date=date(2025, 2, 15), amount=-908.995,
            currency="NZD", merchant="Beta Finance Payoff", category="Debt & Loans", loan_id=borrowed.id,
        ))
        db.commit()

        paid_off = get_loan_status(db, borrowed.id, as_of=date(2025, 2, 15))
        assert paid_off is not None and paid_off.remaining_principal == 0.0
        assert paid_off.payments_remaining == 0 and paid_off.is_payment_due is False
        assert paid_off.payoff_date == date(2025, 2, 15)
        assert paid_off.weekly_payment_equivalent == 0.0 and paid_off.net_worth_impact == 1500.0

        summary = summarize_loans([borrowed, inactive], date(2025, 2, 15), db=db)
        assert len(summary.loans) == 1 and summary.total_liabilities == 0.0
        assert summary.total_asset_value == 1500.0 and summary.net_loan_impact == 1500.0

    engine.dispose()


def _seed_topup_lent_loan(db, acc_id: int) -> int:
    topup_lent = Loan(
        name="Topup Receivable", lender="Jordan Kim", loan_type="lent",
        principal_amount=1000.0, annual_interest_rate=0.12, term_months=0,
        monthly_payment=0.0, start_date=date(2025, 1, 1), currency="NZD", is_active=True,
    )
    db.add(topup_lent)
    db.flush()
    split_topup_tx = _make_tx(
        account_id=acc_id, transaction_date=date(2025, 2, 1), amount=-100.0,
        currency="NZD", merchant="Jordan Kim Topup", category="General",
    )
    db.add_all([
        _make_tx(
            account_id=acc_id, transaction_date=date(2025, 1, 10), amount=-200.0,
            currency="NZD", merchant="Jordan Kim", category="Debt & Loans", loan_id=topup_lent.id,
        ),
        split_topup_tx,
        _make_tx(
            account_id=acc_id, transaction_date=date(2025, 3, 1), amount=150.0,
            currency="NZD", merchant="Jordan Kim Repay", category="Debt & Loans",
            loan_id=topup_lent.id, loan_payment_amount=100.0,
        ),
    ])
    db.flush()
    db.add_all([
        TransactionSplit(transaction_id=split_topup_tx.id, amount=-300.0, category="Debt & Loans", loan_id=topup_lent.id),
        TransactionSplit(transaction_id=split_topup_tx.id, amount=200.0, category="Income"),
    ])
    db.commit()
    return topup_lent.id


def test_lent_open_term_without_disbursements_vs_topup_chronological_interest():
    engine, SessionLocal = _create_test_session()
    with SessionLocal() as db:
        acc = _seed_account(db, name="Checking 2002", currency="NZD")
        open_lent = Loan(
            name="Bridge Receivable", lender="Taylor Lee", loan_type="lent",
            principal_amount=500.0, annual_interest_rate=0.0, term_months=0,
            monthly_payment=0.0, start_date=date(2025, 1, 1), currency="NZD", is_active=True,
        )
        db.add(open_lent)
        db.commit()
        topup_id = _seed_topup_lent_loan(db, acc.id)

        open_status = get_loan_status(db, open_lent.id, as_of=date(2025, 3, 1))
        assert open_status is not None
        assert open_status.remaining_principal == 500.0
        assert open_status.payments_remaining == 0
        assert open_status.is_payment_due is False
        assert open_status.payoff_date is None

        topup_status = get_loan_status(db, topup_id, as_of=date(2025, 3, 1))
        assert topup_status is not None
        assert topup_status.principal_amount == 500.0
        assert topup_status.interest_paid == 5.0
        assert topup_status.principal_paid == 95.0
        assert topup_status.remaining_principal == 405.0
        assert len(topup_status.disbursements) == 2

    engine.dispose()


def test_sgd_loan_repaid_in_nzd_and_partial_override():
    engine, SessionLocal = _create_test_session()
    rate_day = date(2025, 5, 1)
    fetcher = _FakeFxRateFetcher({(rate_day, "NZD", "SGD"): (0.75, "fake_wise")})

    with SessionLocal() as db:
        nzd_acc = _seed_account(db, name="NZD Account", currency="NZD")
        sgd_loan = Loan(
            name="SGD Family Loan", lender="Sam Ng", loan_type="lent",
            principal_amount=500.0, annual_interest_rate=0.0, term_months=0,
            monthly_payment=0.0, start_date=date(2025, 4, 1), currency="SGD", is_active=True,
        )
        db.add(sgd_loan)
        db.flush()

        # 200 NZD inflow with partial 100 NZD loan override -> 100 * 0.75 = 75 SGD applied
        db.add(_make_tx(
            account_id=nzd_acc.id, transaction_date=rate_day, amount=200.0,
            currency="NZD", merchant="Sam Ng", category="Debt & Loans",
            loan_id=sgd_loan.id, loan_payment_amount=100.0,
        ))
        db.commit()

        status = get_loan_status(db, sgd_loan.id, as_of=rate_day, fetcher=fetcher)
        assert status is not None
        assert status.principal_paid == 75.0
        assert status.remaining_principal == 425.0
        assert status.linked_payments[0].applied_amount == 75.0
        assert status.linked_payments[0].amount_nzd == 200.0
        assert len(fetcher.calls) == 1

    engine.dispose()


def test_sgd_loan_repaid_in_usd_direct_triangulated_and_cached():
    engine, SessionLocal = _create_test_session()
    d_direct, d_triang, d_cached = date(2025, 5, 1), date(2025, 5, 2), date(2025, 5, 3)
    fetcher = _FakeFxRateFetcher({
        (d_direct, "USD", "SGD"): (1.35, "fake_wise"),
        (d_triang, "USD", "NZD"): (1.70, "fake_wise"),
        (d_triang, "SGD", "NZD"): (1.30, "fake_wise"),
    })

    with SessionLocal() as db:
        usd_acc = _seed_account(db, name="USD Account", currency="USD")
        db.add(ExchangeRate(rate_date=d_cached, from_currency="USD", to_currency="SGD", rate=1.40, source="cached"))
        sgd_loan = Loan(
            name="SGD Cross-Currency Loan", lender="Morgan Chen", loan_type="lent",
            principal_amount=1000.0, annual_interest_rate=0.0, term_months=0,
            monthly_payment=0.0, start_date=date(2025, 4, 1), currency="SGD", is_active=True,
        )
        db.add(sgd_loan)
        db.flush()

        # 1. Cached USD->SGD rate (100 USD * 1.40 = 140 SGD) -> 0 fetcher calls
        db.add(_make_tx(
            account_id=usd_acc.id, transaction_date=d_cached, amount=100.0,
            currency="USD", merchant="Morgan Chen", category="Debt & Loans", loan_id=sgd_loan.id,
        ))
        db.commit()

        cached_status = get_loan_status(db, sgd_loan.id, as_of=d_cached, fetcher=fetcher)
        assert cached_status is not None and cached_status.principal_paid == 140.0
        assert fetcher.calls == []

        # 2. Direct USD->SGD (100 * 1.35 = 135) + NZD-triangulated (130 * 1.70/1.30 = 170)
        db.add_all([
            _make_tx(account_id=usd_acc.id, transaction_date=d_direct, amount=100.0, currency="USD", merchant="Morgan Chen", category="Debt & Loans", loan_id=sgd_loan.id),
            _make_tx(account_id=usd_acc.id, transaction_date=d_triang, amount=130.0, currency="USD", merchant="Morgan Chen", category="Debt & Loans", loan_id=sgd_loan.id),
        ])
        db.commit()

        full_status = get_loan_status(db, sgd_loan.id, as_of=d_cached, fetcher=fetcher)
        assert full_status is not None and full_status.principal_paid == 445.0
        assert full_status.remaining_principal == 555.0

    engine.dispose()


def test_nzd_loan_repaid_in_sgd_stored_rate_vs_fetcher():
    engine, SessionLocal = _create_test_session()
    d_stored, d_fetch = date(2025, 6, 1), date(2025, 6, 2)
    fetcher = _FakeFxRateFetcher({(d_fetch, "SGD", "NZD"): (1.32, "fake_wise")})

    with SessionLocal() as db:
        sgd_acc = _seed_account(db, name="SGD Account", currency="SGD")
        nzd_loan = Loan(
            name="NZD Loan", lender="Casey Lim", loan_type="lent",
            principal_amount=500.0, annual_interest_rate=0.0, term_months=0,
            monthly_payment=0.0, start_date=date(2025, 5, 1), currency="NZD", is_active=True,
        )
        db.add(nzd_loan)
        db.flush()

        db.add(_make_tx(
            account_id=sgd_acc.id, transaction_date=d_stored, amount=100.0,
            currency="SGD", exchange_rate=1.30, merchant="Casey Lim", category="Debt & Loans", loan_id=nzd_loan.id,
        ))
        db.commit()

        status_stored = get_loan_status(db, nzd_loan.id, as_of=d_stored, fetcher=fetcher)
        assert status_stored is not None and status_stored.principal_paid == 130.0
        assert fetcher.calls == []

        db.add(_make_tx(
            account_id=sgd_acc.id, transaction_date=d_fetch, amount=100.0,
            currency="SGD", exchange_rate=None, merchant="Casey Lim", category="Debt & Loans", loan_id=nzd_loan.id,
        ))
        db.commit()

        status_fetched = get_loan_status(db, nzd_loan.id, as_of=d_fetch, fetcher=fetcher)
        assert status_fetched is not None and status_fetched.principal_paid == 262.0
        assert status_fetched.linked_payments[0].amount_nzd == 132.0
        assert fetcher.calls == [(d_fetch, "SGD", "NZD")]

    engine.dispose()


def test_unresolvable_cross_currency_rate_fails_loud():
    engine, SessionLocal = _create_test_session()
    empty_fetcher = _FakeFxRateFetcher({})
    tx_day = date(2025, 7, 1)

    with SessionLocal() as db:
        usd_acc = _seed_account(db, name="USD Account", currency="USD")
        sgd_loan = Loan(
            name="SGD Strict FX Loan", lender="Riley Tan", loan_type="lent",
            principal_amount=500.0, annual_interest_rate=0.0, term_months=0,
            monthly_payment=0.0, start_date=date(2025, 6, 1), currency="SGD", is_active=True,
        )
        nzd_loan = Loan(
            name="NZD Strict FX Loan", lender="Riley Tan", loan_type="lent",
            principal_amount=500.0, annual_interest_rate=0.0, term_months=0,
            monthly_payment=0.0, start_date=date(2025, 6, 1), currency="NZD", is_active=True,
        )
        db.add_all([sgd_loan, nzd_loan])
        db.flush()

        db.add_all([
            _make_tx(account_id=usd_acc.id, transaction_date=tx_day, amount=100.0, currency="USD", exchange_rate=None, merchant="Riley Tan", category="Debt & Loans", loan_id=sgd_loan.id),
            _make_tx(account_id=usd_acc.id, transaction_date=tx_day, amount=100.0, currency="USD", exchange_rate=None, merchant="Riley Tan", category="Debt & Loans", loan_id=nzd_loan.id),
        ])
        db.commit()

        with pytest.raises(LoanFxConversionError, match="USD->SGD"):
            get_loan_status(db, sgd_loan.id, as_of=tx_day, fetcher=empty_fetcher)
        with pytest.raises(LoanFxConversionError, match="USD->NZD"):
            get_loan_status(db, nzd_loan.id, as_of=tx_day, fetcher=empty_fetcher)

        loaded_sgd = load_loan_with_transactions(db, sgd_loan.id)
        assert loaded_sgd is not None
        with pytest.raises(LoanFxConversionError, match="USD->SGD"):
            compute_loan_status(loaded_sgd, tx_day, db=None, fetcher=empty_fetcher)

    engine.dispose()
