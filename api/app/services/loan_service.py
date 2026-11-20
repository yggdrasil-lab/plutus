"""Amortization schedule, countdown, and net worth calculations for loans."""

import calendar
from datetime import date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import Loan, Transaction
from app.services.fx_service import to_nzd_amount


def add_months(base_date: date, months: int) -> date:
    """Add calendar months while clamping day-of-month to the target month's length."""
    total_months = base_date.month - 1 + months
    year = base_date.year + total_months // 12
    month = total_months % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(base_date.day, last_day))


def load_loans_with_transactions(db: Session, *, active_only: bool = True) -> list[Loan]:
    """Load loans with linked transactions and transaction accounts eagerly joined."""
    stmt = (
        select(Loan)
        .options(joinedload(Loan.transactions).joinedload(Transaction.account))
        .order_by(Loan.start_date.asc(), Loan.id.asc())
    )
    if active_only:
        stmt = stmt.where(Loan.is_active == True)
    return list(db.scalars(stmt).unique().all())


def load_loan_with_transactions(db: Session, loan_id: int) -> Optional[Loan]:
    """Load one loan with linked transactions and transaction accounts eagerly joined."""
    return db.scalar(
        select(Loan)
        .options(joinedload(Loan.transactions).joinedload(Transaction.account))
        .where(Loan.id == loan_id)
    )


def _payment_in_loan_currency(tx: Transaction, loan_currency: str) -> float:
    """Resolve an outflow transaction amount in the loan's currency."""
    tx_currency = (tx.currency or "NZD").strip().upper()
    if tx_currency == loan_currency:
        return abs(float(tx.amount))
    return abs(to_nzd_amount(float(tx.amount), tx_currency, tx.exchange_rate))


def compute_loan_status(
    loan: Loan,
    as_of: date,
    linked_transactions: Optional[list[Transaction]] = None,
) -> dict[str, object]:
    """Compute remaining principal, payment countdown, and net worth impact from linked outflows."""
    principal = float(loan.principal_amount)
    annual_rate = float(loan.annual_interest_rate)
    monthly_rate = annual_rate / 12.0
    term = int(loan.term_months)
    monthly_pmt = float(loan.monthly_payment)
    asset_val = float(loan.asset_value) if loan.asset_value is not None else 0.0
    loan_currency = (loan.currency or "NZD").strip().upper()

    source_txs = linked_transactions if linked_transactions is not None else (loan.transactions or [])
    outflows = sorted(
        (tx for tx in source_txs if float(tx.amount) < 0),
        key=lambda tx: (tx.transaction_date, tx.id or 0),
    )

    balance = principal
    payments_made = 0
    interest_paid_total = 0.0
    total_paid = 0.0

    for tx in outflows:
        payment_amt = _payment_in_loan_currency(tx, loan_currency)
        interest = balance * monthly_rate
        principal_part = max(0.0, payment_amt - interest)
        balance = max(0.0, balance - principal_part)
        if balance <= 0.01:
            balance = 0.0
        interest_paid_total += interest
        total_paid += payment_amt
        payments_made += 1

    remaining_principal = round(balance, 2)
    principal_paid = round(max(0.0, principal - remaining_principal), 2)
    interest_paid = round(interest_paid_total, 2)
    total_paid_amount = round(total_paid, 2)
    payments_remaining = 0 if balance <= 0.0 else max(0, term - payments_made)
    next_payment_date: Optional[date] = (
        add_months(loan.start_date, payments_made) if payments_remaining > 0 else None
    )
    is_payment_due = bool(
        payments_remaining > 0 and next_payment_date is not None and next_payment_date <= as_of
    )
    last_payment_date: Optional[date] = outflows[-1].transaction_date if outflows else None
    payoff_date = (
        last_payment_date
        if (payments_remaining == 0 and last_payment_date is not None)
        else add_months(loan.start_date, max(0, term - 1))
    )
    remaining_interest = round(
        max(0.0, (monthly_pmt * payments_remaining) - remaining_principal), 2
    )
    weekly_equiv = round(monthly_pmt * 12.0 / 52.0, 2) if payments_remaining > 0 else 0.0
    net_worth_impact = round(asset_val - remaining_principal, 2)

    linked_payments = [
        {
            "id": tx.id,
            "transaction_date": tx.transaction_date,
            "amount": float(tx.amount),
            "currency": (tx.currency or "NZD").strip().upper(),
            "amount_nzd": round(abs(to_nzd_amount(float(tx.amount), tx.currency, tx.exchange_rate)), 2),
            "merchant": tx.merchant,
            "account_id": tx.account_id,
            "account_name": tx.account.name if tx.account else None,
            "category": tx.category,
        }
        for tx in reversed(outflows)
    ]

    return {
        "id": loan.id,
        "name": loan.name,
        "lender": loan.lender,
        "principal_amount": principal,
        "annual_interest_rate": annual_rate,
        "term_months": term,
        "monthly_payment": monthly_pmt,
        "start_date": loan.start_date,
        "asset_value": float(loan.asset_value) if loan.asset_value is not None else None,
        "currency": loan.currency,
        "notes": loan.notes,
        "is_active": loan.is_active,
        "payments_made": payments_made,
        "payments_remaining": payments_remaining,
        "payoff_date": payoff_date,
        "next_payment_date": next_payment_date,
        "remaining_principal": remaining_principal,
        "principal_paid": principal_paid,
        "total_paid_amount": total_paid_amount,
        "interest_paid": interest_paid,
        "remaining_interest": remaining_interest,
        "weekly_payment_equivalent": weekly_equiv,
        "net_worth_impact": net_worth_impact,
        "last_payment_date": last_payment_date,
        "is_payment_due": is_payment_due,
        "linked_payments": linked_payments,
    }


def summarize_loans(loans: list[Loan], as_of: date) -> dict[str, object]:
    """Compute individual loan statuses and portfolio-level liability totals."""
    items = [compute_loan_status(loan, as_of) for loan in loans if loan.is_active]
    total_liabilities = round(sum(float(i["remaining_principal"]) for i in items), 2)
    total_asset_value = round(sum(float(i["asset_value"] or 0.0) for i in items), 2)
    total_monthly_payment = round(
        sum(float(i["monthly_payment"]) for i in items if int(i["payments_remaining"]) > 0), 2
    )
    total_weekly_payment = round(
        sum(float(i["weekly_payment_equivalent"]) for i in items), 2
    )
    payoff_dates = [i["payoff_date"] for i in items if int(i["payments_remaining"]) > 0]
    latest_payoff = max(payoff_dates) if payoff_dates else None

    return {
        "loans": items,
        "total_liabilities": total_liabilities,
        "total_asset_value": total_asset_value,
        "net_loan_impact": round(total_asset_value - total_liabilities, 2),
        "total_monthly_payment": total_monthly_payment,
        "total_weekly_payment": total_weekly_payment,
        "latest_payoff_date": latest_payoff,
    }
