"""Amortization schedule, countdown, and net worth calculations for loans."""

import calendar
from datetime import date
from typing import Optional

from app.models import Loan


def add_months(base_date: date, months: int) -> date:
    """Add calendar months while clamping day-of-month to the target month's length."""
    total_months = base_date.month - 1 + months
    year = base_date.year + total_months // 12
    month = total_months % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(base_date.day, last_day))


def compute_loan_status(loan: Loan, as_of: date) -> dict[str, object]:
    """Compute remaining principal, payment countdown, and net worth impact for a loan."""
    principal = float(loan.principal_amount)
    annual_rate = float(loan.annual_interest_rate)
    monthly_rate = annual_rate / 12.0
    term = int(loan.term_months)
    monthly_pmt = float(loan.monthly_payment)
    asset_val = float(loan.asset_value) if loan.asset_value is not None else 0.0

    payments_made = 0
    for idx in range(term):
        due = add_months(loan.start_date, idx)
        if due <= as_of:
            payments_made += 1
        else:
            break

    payments_remaining = max(0, term - payments_made)
    payoff_date = add_months(loan.start_date, max(0, term - 1))
    next_payment_date: Optional[date] = (
        add_months(loan.start_date, payments_made) if payments_remaining > 0 else None
    )

    balance = principal
    for _ in range(payments_made):
        interest = balance * monthly_rate
        principal_part = monthly_pmt - interest
        balance = max(0.0, balance - principal_part)
        if balance <= 0.01:
            balance = 0.0
            break

    if payments_remaining == 0:
        balance = 0.0

    remaining_principal = round(balance, 2)
    principal_paid = round(max(0.0, principal - remaining_principal), 2)
    remaining_interest = round(
        max(0.0, (monthly_pmt * payments_remaining) - remaining_principal), 2
    )
    weekly_equiv = round(monthly_pmt * 12.0 / 52.0, 2) if payments_remaining > 0 else 0.0
    net_worth_impact = round(asset_val - remaining_principal, 2)

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
        "remaining_interest": remaining_interest,
        "weekly_payment_equivalent": weekly_equiv,
        "net_worth_impact": net_worth_impact,
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
