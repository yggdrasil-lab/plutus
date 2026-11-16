"""Shared recurrence amortization helpers for weekly budgets and runway burn."""

from datetime import date, timedelta
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Transaction

RECURRENCE_WEEKLY_FACTOR: dict[str, float] = {
    "weekly": 1.0,
    "fortnightly": 0.5,
    "monthly": 12.0 / 52.0,
    "quarterly": 4.0 / 52.0,
    "annual": 1.0 / 52.0,
}

RECURRENCE_LOOKBACK_DAYS: dict[str, int] = {
    "weekly": 28,
    "fortnightly": 28,
    "monthly": 35,
    "quarterly": 95,
    "annual": 366,
}


def get_active_recurring_commitments(
    db: Session,
    as_of: date,
) -> list[tuple[Transaction, float]]:
    """Return latest non-transfer expense per (merchant, recurrence) with weekly NZD cost."""
    max_lookback = as_of - timedelta(days=366)
    rows = db.scalars(
        select(Transaction)
        .where(
            Transaction.transaction_date >= max_lookback,
            Transaction.transaction_date <= as_of,
            Transaction.amount < 0,
            Transaction.recurrence.in_(tuple(RECURRENCE_WEEKLY_FACTOR.keys())),
            func.lower(Transaction.category).not_in(("transfers", "transfer")),
        )
        .order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
    ).all()

    seen: set[tuple[str, str]] = set()
    commitments: list[tuple[Transaction, float]] = []
    for tx in rows:
        rec = (tx.recurrence or "").strip().lower()
        factor = RECURRENCE_WEEKLY_FACTOR.get(rec)
        if not factor:
            continue
        if (as_of - tx.transaction_date).days > RECURRENCE_LOOKBACK_DAYS[rec]:
            continue
        key = ((tx.merchant or "").strip().lower(), rec)
        if key in seen:
            continue
        seen.add(key)
        amt_nzd = abs(float(tx.amount)) * float(tx.exchange_rate or 1.0)
        commitments.append((tx, round(amt_nzd * factor, 2)))
    return commitments
