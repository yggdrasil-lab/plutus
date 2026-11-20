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


def _payment_in_loan_currency(
    tx: Transaction,
    loan_currency: str,
    raw_amount: Optional[float] = None,
) -> float:
    """Resolve a transaction or partial override amount in the loan's currency."""
    amount = abs(float(tx.amount if raw_amount is None else raw_amount))
    tx_currency = (tx.currency or "NZD").strip().upper()
    if tx_currency == loan_currency:
        return amount
    if loan_currency == "NZD":
        return abs(to_nzd_amount(amount, tx_currency, tx.exchange_rate))
    return amount


def _serialize_loan_entry(
    tx: Transaction,
    *,
    applied_amount: float,
    entry_type: str,
) -> dict[str, object]:
    """Serialize one linked loan transaction entry (repayment or disbursement)."""
    return {
        "id": tx.id,
        "transaction_date": tx.transaction_date,
        "amount": float(tx.amount),
        "currency": (tx.currency or "NZD").strip().upper(),
        "amount_nzd": round(abs(to_nzd_amount(float(tx.amount), tx.currency, tx.exchange_rate)), 2),
        "applied_amount": round(applied_amount, 2),
        "loan_payment_amount": (
            float(tx.loan_payment_amount) if tx.loan_payment_amount is not None else None
        ),
        "entry_type": entry_type,
        "merchant": tx.merchant,
        "account_id": tx.account_id,
        "account_name": tx.account.name if tx.account else None,
        "category": tx.category,
    }


def compute_loan_status(
    loan: Loan,
    as_of: date,
    linked_transactions: Optional[list[Transaction]] = None,
) -> dict[str, object]:
    """Compute remaining principal, payment countdown, and net worth impact from linked transactions."""
    principal = float(loan.principal_amount)
    annual_rate = float(loan.annual_interest_rate)
    monthly_rate = annual_rate / 12.0
    term = int(loan.term_months)
    monthly_pmt = float(loan.monthly_payment)
    asset_val = float(loan.asset_value) if loan.asset_value is not None else 0.0
    loan_currency = (loan.currency or "NZD").strip().upper()
    loan_type = (getattr(loan, "loan_type", None) or "borrowed").strip().lower()
    if loan_type not in ("borrowed", "lent"):
        loan_type = "borrowed"

    source_txs = linked_transactions if linked_transactions is not None else (loan.transactions or [])
    outflows = sorted(
        (tx for tx in source_txs if float(tx.amount) < 0),
        key=lambda tx: (tx.transaction_date, tx.id or 0),
    )
    inflows = sorted(
        (tx for tx in source_txs if float(tx.amount) > 0),
        key=lambda tx: (tx.transaction_date, tx.id or 0),
    )

    repayment_txs = inflows if loan_type == "lent" else outflows
    disbursement_txs = outflows if loan_type == "lent" else []

    balance = principal
    payments_made = 0
    interest_paid_total = 0.0
    total_paid = 0.0
    repayment_entries: list[dict[str, object]] = []

    for tx in repayment_txs:
        applied = _payment_in_loan_currency(
            tx,
            loan_currency,
            raw_amount=(
                abs(float(tx.loan_payment_amount))
                if tx.loan_payment_amount is not None
                else None
            ),
        )
        interest = balance * monthly_rate if annual_rate > 0 else 0.0
        principal_part = max(0.0, applied - interest)
        balance = max(0.0, balance - principal_part)
        if balance <= 0.01:
            balance = 0.0
        interest_paid_total += interest
        total_paid += applied
        payments_made += 1
        repayment_entries.append(
            _serialize_loan_entry(tx, applied_amount=applied, entry_type="repayment")
        )

    linked_payments = list(reversed(repayment_entries))
    disbursements = [
        _serialize_loan_entry(
            tx,
            applied_amount=_payment_in_loan_currency(tx, loan_currency),
            entry_type="disbursement",
        )
        for tx in reversed(disbursement_txs)
    ]

    remaining_principal = round(balance, 2)
    principal_paid = round(max(0.0, principal - remaining_principal), 2)
    interest_paid = round(interest_paid_total, 2)
    total_paid_amount = round(total_paid, 2)
    last_payment_date: Optional[date] = repayment_txs[-1].transaction_date if repayment_txs else None

    if loan_type == "lent" and term == 0:
        payments_remaining = 0
        next_payment_date: Optional[date] = None
        is_payment_due = False
        payoff_date: Optional[date] = last_payment_date if balance <= 0.0 else None
        remaining_interest = 0.0
        weekly_equiv = 0.0
    else:
        payments_remaining = 0 if balance <= 0.0 else max(0, term - payments_made)
        next_payment_date = (
            add_months(loan.start_date, payments_made) if payments_remaining > 0 else None
        )
        is_payment_due = bool(
            payments_remaining > 0 and next_payment_date is not None and next_payment_date <= as_of
        )
        payoff_date = (
            last_payment_date
            if (payments_remaining == 0 and last_payment_date is not None)
            else add_months(loan.start_date, max(0, term - 1))
        )
        remaining_interest = round(
            max(0.0, (monthly_pmt * payments_remaining) - remaining_principal), 2
        )
        weekly_equiv = round(monthly_pmt * 12.0 / 52.0, 2) if payments_remaining > 0 else 0.0

    net_worth_impact = 0.0 if loan_type == "lent" else round(asset_val - remaining_principal, 2)

    return {
        "id": loan.id,
        "name": loan.name,
        "lender": loan.lender,
        "loan_type": loan_type,
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
        "disbursements": disbursements,
    }


def summarize_loans(loans: list[Loan], as_of: date) -> dict[str, object]:
    """Compute individual loan statuses and portfolio-level liability/receivable totals."""
    items = [compute_loan_status(loan, as_of) for loan in loans if loan.is_active]
    borrowed_items = [i for i in items if i["loan_type"] == "borrowed"]
    total_liabilities = round(sum(float(i["remaining_principal"]) for i in borrowed_items), 2)
    total_receivables = round(
        sum(float(i["remaining_principal"]) for i in items if i["loan_type"] == "lent"), 2
    )
    total_asset_value = round(sum(float(i["asset_value"] or 0.0) for i in borrowed_items), 2)
    total_monthly_payment = round(
        sum(float(i["monthly_payment"]) for i in borrowed_items if int(i["payments_remaining"]) > 0),
        2,
    )
    total_weekly_payment = round(
        sum(float(i["weekly_payment_equivalent"]) for i in borrowed_items), 2
    )
    payoff_dates = [
        i["payoff_date"]
        for i in borrowed_items
        if int(i["payments_remaining"]) > 0 and i["payoff_date"] is not None
    ]
    latest_payoff = max(payoff_dates) if payoff_dates else None

    return {
        "loans": items,
        "total_liabilities": total_liabilities,
        "total_receivables": total_receivables,
        "total_asset_value": total_asset_value,
        "net_loan_impact": round(total_asset_value - total_liabilities, 2),
        "total_monthly_payment": total_monthly_payment,
        "total_weekly_payment": total_weekly_payment,
        "latest_payoff_date": latest_payoff,
    }
