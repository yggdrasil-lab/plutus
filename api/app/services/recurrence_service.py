"""Shared recurrence amortization helpers for weekly budgets and runway burn."""

from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models import Transaction, TransactionSplit
from app.services.fx_service import to_nzd_amount

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


def get_period_amortized_recurrences(period: Optional[str]) -> frozenset[str]:
    """Return recurrence keys amortized into category totals for a named period."""
    if period in ("week", "last_week"):
        return frozenset(RECURRENCE_WEEKLY_FACTOR.keys())
    if period in ("month", "last_month"):
        return frozenset(("annual", "quarterly"))
    return frozenset()


def is_recurrence_excluded_from_period(
    period: Optional[str],
    recurrence: Optional[str],
) -> bool:
    """Return True if a raw transaction's recurrence is excluded from direct period sums."""
    rec = (recurrence or "").strip().lower()
    if not rec:
        return False
    if period in ("week", "last_week"):
        return True
    if period in ("month", "last_month"):
        return rec in ("annual", "quarterly", "one-off")
    return False


def get_active_recurring_commitments(
    db: Session,
    as_of: date,
    *,
    eager_load: bool = False,
) -> list[tuple[Transaction, float]]:
    """Return latest non-transfer expense per (merchant, recurrence) with weekly NZD cost."""
    max_lookback = as_of - timedelta(days=366)
    stmt = (
        select(Transaction)
        .where(
            Transaction.transaction_date >= max_lookback,
            Transaction.transaction_date <= as_of,
            Transaction.amount < 0,
            Transaction.recurrence.in_(tuple(RECURRENCE_WEEKLY_FACTOR.keys())),
            func.lower(Transaction.category).not_in(("transfers", "transfer")),
        )
        .order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
    )
    if eager_load:
        stmt = stmt.options(
            joinedload(Transaction.account),
            joinedload(Transaction.counterpart_account),
            joinedload(Transaction.loan),
            joinedload(Transaction.linked_transaction).joinedload(Transaction.account),
            joinedload(Transaction.splits).joinedload(TransactionSplit.loan),
            joinedload(Transaction.receipt),
        )
        rows = db.scalars(stmt).unique().all()
    else:
        stmt = stmt.options(
            joinedload(Transaction.loan),
            joinedload(Transaction.splits).joinedload(TransactionSplit.loan),
        )
        rows = db.scalars(stmt).unique().all()

    seen: set[tuple[str, str]] = set()
    commitments: list[tuple[Transaction, float]] = []
    for tx in rows:
        splits = getattr(tx, "splits", None) or []
        if splits:
            non_excluded_splits = [
                s
                for s in splits
                if (s.category or "").strip().lower() not in ("transfers", "transfer")
                and not (
                    s.loan is not None
                    and (s.loan.loan_type or "").strip().lower() == "lent"
                )
            ]
            if not non_excluded_splits:
                continue
            effective_amount = sum(abs(float(s.amount)) for s in non_excluded_splits)
        else:
            if tx.loan is not None and (tx.loan.loan_type or "").strip().lower() == "lent":
                continue
            effective_amount = tx.amount
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
        amt_nzd = to_nzd_amount(effective_amount, tx.currency, tx.exchange_rate)
        commitments.append((tx, round(amt_nzd * factor, 2)))
    return commitments
