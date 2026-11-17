"""Shared date-range resolution for transaction and budget period filters."""

from datetime import date, timedelta
from typing import Optional

from fastapi import HTTPException

VALID_PERIODS = ("week", "last_week", "month", "last_month", "all")


def resolve_period_dates(
    period: Optional[str],
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    *,
    today: Optional[date] = None,
) -> tuple[Optional[date], Optional[date]]:
    """Resolve a named period filter into inclusive (start_date, end_date) bounds."""
    if not period:
        if start_date and end_date and start_date > end_date:
            raise HTTPException(status_code=400, detail="start_date must be on or before end_date.")
        return start_date, end_date

    ref = today or date.today()
    if period == "week":
        week_start = ref - timedelta(days=ref.weekday())
        return week_start, week_start + timedelta(days=6)
    if period == "last_week":
        this_monday = ref - timedelta(days=ref.weekday())
        return this_monday - timedelta(days=7), this_monday - timedelta(days=1)
    if period == "month":
        return date(ref.year, ref.month, 1), ref
    if period == "last_month":
        first_this_month = date(ref.year, ref.month, 1)
        prev_end = first_this_month - timedelta(days=1)
        return date(prev_end.year, prev_end.month, 1), prev_end
    if period == "all":
        return None, None

    raise HTTPException(
        status_code=400,
        detail="Invalid period. Choose 'week', 'last_week', 'month', 'last_month', or 'all'.",
    )
