"""ReportingWindow resolution, cadence conversion, and SQL window queries."""

from datetime import date, timedelta
from typing import Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.models import Receipt, Transaction, TransactionSplit
from app.services.spending_projection._types import (
    DAYS_PER_YEAR,
    MONTHLY_CADENCE_PERIODS,
    MONTHS_PER_YEAR,
    RECURRENCE_WEEKLY_FACTOR,
    VALID_PERIODS,
    WEEKLY_CADENCE_PERIODS,
    WEEKS_PER_YEAR,
    InvalidWindowError,
    ReportingWindow,
    ResolvedWindow,
)


def weekly_to_monthly(weekly_amount: float) -> float:
    """Convert a weekly NZD amount to its 52/12 monthly equivalent."""
    return round(float(weekly_amount) * WEEKS_PER_YEAR / MONTHS_PER_YEAR, 2)


def weekly_to_annual(weekly_amount: float) -> float:
    """Convert a weekly NZD amount to its 52-week annual equivalent."""
    return round(float(weekly_amount) * WEEKS_PER_YEAR, 2)


def monthly_to_weekly(monthly_amount: float) -> float:
    """Convert a monthly NZD amount to its 12/52 weekly equivalent."""
    return round(float(monthly_amount) * MONTHS_PER_YEAR / WEEKS_PER_YEAR, 2)


def annual_to_weekly(annual_amount: float) -> float:
    """Convert an annual NZD amount to its 52-week amortized weekly equivalent."""
    return round(float(annual_amount) / WEEKS_PER_YEAR, 2)


def get_period_amortized_recurrences(period: Optional[str]) -> frozenset[str]:
    """Return recurrence keys amortized into category totals for a named period."""
    if period in WEEKLY_CADENCE_PERIODS:
        return frozenset(RECURRENCE_WEEKLY_FACTOR.keys())
    if period in MONTHLY_CADENCE_PERIODS:
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
    if period in WEEKLY_CADENCE_PERIODS:
        return True
    if period in MONTHLY_CADENCE_PERIODS:
        return rec in ("annual", "quarterly", "one-off")
    return False


def _compute_named_period_bounds(
    period: str,
    ref: date,
) -> tuple[date | None, date | None]:
    """Compute inclusive (start_date, end_date) for a validated named period."""
    if period == "week":
        week_start = ref - timedelta(days=ref.weekday())
        return week_start, week_start + timedelta(days=6)
    if period == "last_week":
        this_monday = ref - timedelta(days=ref.weekday())
        return this_monday - timedelta(days=7), this_monday - timedelta(days=1)
    if period == "month":
        return date(ref.year, ref.month, 1), ref
    if period == "last_month":
        prev_end = date(ref.year, ref.month, 1) - timedelta(days=1)
        return date(prev_end.year, prev_end.month, 1), prev_end
    if period in ("annual", "year"):
        return ref - timedelta(days=DAYS_PER_YEAR), ref
    return None, None


def resolve_reporting_window(
    window: ReportingWindow,
    *,
    reference_date: date | None = None,
) -> ResolvedWindow:
    """Validate and resolve a ReportingWindow into concrete bounds and cadence flags."""
    ref = reference_date or date.today()
    raw_period = window.period.strip() if isinstance(window.period, str) else window.period
    if raw_period is not None and raw_period not in VALID_PERIODS:
        raise InvalidWindowError(
            "Invalid period. Choose 'week', 'last_week', 'month', 'last_month', 'annual', or 'all'."
        )

    has_custom_bounds = window.start_date is not None or window.end_date is not None
    if has_custom_bounds:
        if window.start_date and window.end_date and window.start_date > window.end_date:
            raise InvalidWindowError("start_date must be on or before end_date.")
        effective_period = None
        start_date, end_date = window.start_date, window.end_date
    elif not raw_period:
        effective_period = None
        start_date, end_date = None, None
    else:
        effective_period = raw_period
        start_date, end_date = _compute_named_period_bounds(effective_period, ref)

    is_weekly = effective_period in WEEKLY_CADENCE_PERIODS
    is_monthly = effective_period in MONTHLY_CADENCE_PERIODS
    return ResolvedWindow(
        period_label=effective_period or "custom",
        effective_period=effective_period,
        start_date=start_date,
        end_date=end_date,
        reference_date=ref,
        as_of_date=end_date or ref,
        is_weekly_cadence=is_weekly,
        is_monthly_cadence=is_monthly,
        is_budget_cadence_window=(is_weekly or is_monthly),
        amortized_recurrences=get_period_amortized_recurrences(effective_period),
    )


resolve_window = resolve_reporting_window


def _transaction_eager_options(
    *,
    include_receipt_items: bool = True,
    include_relations: bool = False,
):
    """Return consistent SQLAlchemy joinedload options for projection and snapshot hydration."""
    receipt_load = (
        joinedload(Transaction.receipt).joinedload(Receipt.items)
        if include_receipt_items
        else joinedload(Transaction.receipt)
    )
    options = [
        receipt_load,
        joinedload(Transaction.loan),
        joinedload(Transaction.splits).joinedload(TransactionSplit.loan),
    ]
    if include_relations:
        options.extend(
            [
                joinedload(Transaction.account),
                joinedload(Transaction.counterpart_account),
                joinedload(Transaction.linked_transaction).joinedload(Transaction.account),
            ]
        )
    return tuple(options)


def _build_window_filter_clauses(
    resolved: ResolvedWindow,
    *,
    account_id: Optional[int] = None,
    loan_id: Optional[int] = None,
) -> list:
    """Build SQLAlchemy WHERE predicates for resolved date bounds, account, and loan filters."""
    clauses = []
    if account_id is not None:
        clauses.append(
            or_(
                Transaction.account_id == account_id,
                and_(
                    Transaction.counterpart_account_id == account_id,
                    Transaction.linked_transaction_id.is_(None),
                ),
            )
        )
    if loan_id is not None:
        clauses.append(
            or_(
                Transaction.loan_id == loan_id,
                Transaction.splits.any(TransactionSplit.loan_id == loan_id),
            )
        )
    if resolved.start_date is not None:
        clauses.append(Transaction.transaction_date >= resolved.start_date)
    if resolved.end_date is not None:
        clauses.append(Transaction.transaction_date <= resolved.end_date)
    return clauses


def _query_window_transactions(
    db: Session,
    resolved: ResolvedWindow,
    *,
    account_id: Optional[int] = None,
    loan_id: Optional[int] = None,
    include_relations: bool = False,
) -> list[Transaction]:
    """Query transactions within the resolved ReportingWindow bounds with eager relations."""
    clauses = _build_window_filter_clauses(resolved, account_id=account_id, loan_id=loan_id)
    stmt = (
        select(Transaction)
        .options(
            *_transaction_eager_options(
                include_receipt_items=True,
                include_relations=include_relations,
            )
        )
        .where(*clauses)
        .order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
    )
    return list(db.scalars(stmt).unique().all())


def _query_paginated_window_transactions(
    db: Session,
    resolved: ResolvedWindow,
    *,
    account_id: Optional[int] = None,
    loan_id: Optional[int] = None,
    offset: int = 0,
    limit: int = 100,
) -> tuple[list[Transaction], int]:
    """Query one page of eager-loaded transactions and exact total row count without join inflation."""
    clauses = _build_window_filter_clauses(resolved, account_id=account_id, loan_id=loan_id)
    id_stmt = select(Transaction.id).where(*clauses)
    total_count = db.scalar(select(func.count()).select_from(id_stmt.subquery())) or 0
    if total_count == 0:
        return [], 0
    page_ids = list(
        db.scalars(
            id_stmt.order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
    )
    if not page_ids:
        return [], int(total_count)
    rows_stmt = (
        select(Transaction)
        .options(*_transaction_eager_options(include_receipt_items=True, include_relations=True))
        .where(Transaction.id.in_(page_ids))
        .order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
    )
    return list(db.scalars(rows_stmt).unique().all()), int(total_count)
