"""Weekly burn, category burn estimates, recurring commitments, and current-week burn projection."""

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Transaction, TransactionSplit
from app.services.category_taxonomy import get_parent_category
from app.services.spending_projection._lines import (
    _iter_active_recurring_allocations,
    _load_annual_metadata,
    expand_expense_buckets,
)
from app.services.spending_projection._types import (
    DAYS_PER_YEAR,
    DEFAULT_WEEKLY_BURN_NZD,
    TRANSFER_CATEGORIES,
    WEEKS_PER_YEAR,
    BurnProjection,
    CategoryBurnEstimate,
    InvalidWindowError,
    RecurringCommitmentDetail,
    ReportingWindow,
    WeeklyCategoryBurn,
    _AnnualCategoryMetadata,
)
from app.services.spending_projection._window import (
    _transaction_eager_options,
    resolve_reporting_window,
    weekly_to_annual,
    weekly_to_monthly,
)


@dataclass
class _VariableSpendTotals:
    """Mutable non-recurring spend totals across lookback, 365-day annual, and this week."""

    canonical_names: dict[str, str]
    lookback_totals: dict[str, float]
    annual_totals: dict[str, float]
    this_week_regular: dict[str, float]


def _query_variable_burn_transactions(
    db: Session,
    start_date: date,
    as_of: date,
) -> list[Transaction]:
    """Fetch non-recurring, non-transfer candidate outflow rows in [start_date, as_of]."""
    stmt = (
        select(Transaction)
        .options(*_transaction_eager_options(include_receipt_items=True, include_relations=False))
        .where(
            Transaction.transaction_date >= start_date,
            Transaction.transaction_date <= as_of,
            or_(Transaction.amount < 0, Transaction.splits.any(TransactionSplit.amount < 0)),
            Transaction.recurrence.is_(None),
            or_(
                func.lower(Transaction.category).not_in(TRANSFER_CATEGORIES),
                Transaction.splits.any(),
            ),
        )
    )
    return list(db.scalars(stmt).unique().all())


def _record_variable_bucket(
    totals: _VariableSpendTotals,
    cat_name: str,
    amt_nzd: float,
    *,
    tx_date: date,
    meta: _AnnualCategoryMetadata,
    lookback_start: date,
    annual_start: date,
    start_of_week: date,
) -> None:
    """Record one expanded expense bucket into annual 365d, lookback, or current-week totals."""
    key = cat_name.lower()
    display_name = meta.canonical_name_map.get(key) or cat_name
    if key in meta.annual_cats:
        if tx_date >= annual_start:
            totals.canonical_names.setdefault(key, display_name)
            totals.annual_totals[key] = totals.annual_totals.get(key, 0.0) + amt_nzd
        return
    if tx_date >= lookback_start:
        totals.canonical_names.setdefault(key, display_name)
        totals.lookback_totals[key] = totals.lookback_totals.get(key, 0.0) + amt_nzd
    if tx_date >= start_of_week:
        totals.canonical_names.setdefault(key, display_name)
        totals.this_week_regular[key] = totals.this_week_regular.get(key, 0.0) + amt_nzd


def _collect_variable_spend(
    db: Session,
    meta: _AnnualCategoryMetadata,
    *,
    lookback_start: date,
    annual_start: date,
    start_of_week: date,
    as_of: date,
) -> _VariableSpendTotals:
    """Aggregate variable non-recurring expenses using canonical ExpenseLine expansion."""
    min_start = min(lookback_start, annual_start) if meta.annual_cats else lookback_start
    txs = _query_variable_burn_transactions(db, min_start, as_of)
    totals = _VariableSpendTotals({}, {}, {}, {})
    for tx in txs:
        tx_date = tx.transaction_date
        for cat_name, amt_nzd in expand_expense_buckets(tx):
            _record_variable_bucket(
                totals,
                cat_name,
                amt_nzd,
                tx_date=tx_date,
                meta=meta,
                lookback_start=lookback_start,
                annual_start=annual_start,
                start_of_week=start_of_week,
            )
    return totals


def _collect_recurring_burn(
    db: Session,
    as_of: date,
    meta: _AnnualCategoryMetadata,
    canonical_names: dict[str, str],
) -> tuple[dict[str, float], tuple[RecurringCommitmentDetail, ...], float]:
    """Collect per-split recurring weekly burn totals and itemized commitment details."""
    allocs = [alloc for _, alloc, _ in _iter_active_recurring_allocations(db, as_of)]
    total_rec_wk = sum(alloc.weekly_nzd for alloc in allocs)
    recurring_totals: dict[str, float] = {}
    details: list[RecurringCommitmentDetail] = []
    for alloc in allocs:
        key = alloc.category.lower()
        display_name = meta.canonical_name_map.get(key) or alloc.category
        canonical_names.setdefault(key, display_name)
        recurring_totals[key] = recurring_totals.get(key, 0.0) + alloc.weekly_nzd
        pct = round(alloc.weekly_nzd / total_rec_wk * 100.0, 1) if total_rec_wk > 0 else 0.0
        details.append(
            RecurringCommitmentDetail(
                transaction_id=alloc.transaction_id,
                merchant=alloc.merchant,
                category=display_name,
                recurrence=alloc.recurrence,
                last_charged_date=alloc.last_charged_date,
                original_amount=alloc.original_amount,
                currency=alloc.currency,
                amount_nzd=alloc.amount_nzd,
                weekly_nzd=alloc.weekly_nzd,
                monthly_nzd=alloc.monthly_nzd,
                percentage_of_recurring=pct,
            )
        )
    details.sort(key=lambda item: (-item.weekly_nzd, item.merchant.lower(), item.category.lower()))
    return recurring_totals, tuple(details), round(total_rec_wk, 2)


def _resolve_category_period(key: str, meta: _AnnualCategoryMetadata) -> str:
    """Resolve effective budget cadence ('weekly', 'monthly', or 'annual') for a category."""
    if key in meta.annual_cats:
        return "annual"
    rule = meta.raw_budget_rules.get(key)
    return (rule.budget_period or "monthly").strip().lower() if rule else "monthly"


def _build_category_burn_estimates(
    totals: _VariableSpendTotals,
    recurring_totals: dict[str, float],
    meta: _AnnualCategoryMetadata,
    lookback_weeks: int,
) -> tuple[CategoryBurnEstimate, ...]:
    """Build sorted CategoryBurnEstimate rows from variable, annual, and recurring burn."""
    raw_rows: list[tuple[str, str, float, float, float]] = []
    for key, display_name in totals.canonical_names.items():
        var_source = (
            totals.annual_totals.get(key, 0.0) / WEEKS_PER_YEAR
            if key in meta.annual_cats
            else totals.lookback_totals.get(key, 0.0) / float(lookback_weeks)
        )
        var_wk = round(var_source, 2)
        rec_wk = round(recurring_totals.get(key, 0.0), 2)
        avg_wk = round(var_wk + rec_wk, 2)
        raw_rows.append((key, display_name, var_wk, rec_wk, avg_wk))

    total_var_wk = sum(item[2] for item in raw_rows)
    total_avg_wk = sum(item[4] for item in raw_rows)
    estimates = [
        CategoryBurnEstimate(
            category=display_name,
            parent_category=get_parent_category(display_name, meta.parent_map),
            variable_weekly_burn=var_wk,
            recurring_weekly_burn=rec_wk,
            average_weekly_burn=avg_wk,
            suggested_monthly_limit=weekly_to_monthly(avg_wk),
            suggested_annual_limit=weekly_to_annual(avg_wk),
            budget_period=_resolve_category_period(key, meta),
            variable_percentage=round(var_wk / total_var_wk * 100.0, 1) if total_var_wk > 0 else 0.0,
            total_percentage=round(avg_wk / total_avg_wk * 100.0, 1) if total_avg_wk > 0 else 0.0,
        )
        for key, display_name, var_wk, rec_wk, avg_wk in raw_rows
    ]
    estimates.sort(key=lambda item: (-item.average_weekly_burn, item.category))
    return tuple(estimates)


def _build_this_week_breakdown(
    totals: _VariableSpendTotals,
    recurring_totals: dict[str, float],
) -> tuple[float, float, tuple[WeeklyCategoryBurn, ...]]:
    """Compute current-week variable spend, total spend, and reconciled per-category breakdown."""
    var_by_cat: dict[str, float] = dict(totals.this_week_regular)
    for key, annual_spend in totals.annual_totals.items():
        annual_wk = round(annual_spend / WEEKS_PER_YEAR, 2)
        if annual_wk > 0:
            var_by_cat[key] = var_by_cat.get(key, 0.0) + annual_wk

    this_week_variable_spend = round(sum(var_by_cat.values()), 2)
    categories: list[WeeklyCategoryBurn] = []
    for key in set(var_by_cat.keys()) | set(recurring_totals.keys()):
        spend_nzd = round(var_by_cat.get(key, 0.0) + recurring_totals.get(key, 0.0), 2)
        if spend_nzd > 0:
            categories.append(WeeklyCategoryBurn(category=totals.canonical_names[key], spend_nzd=spend_nzd))

    categories.sort(key=lambda item: (-item.spend_nzd, item.category))
    this_week_spend = round(sum(item.spend_nzd for item in categories), 2)
    return this_week_variable_spend, this_week_spend, tuple(categories)


def _resolve_effective_burn(
    observed_weekly_burn: float,
    meta: _AnnualCategoryMetadata,
) -> tuple[float, float]:
    """Apply canonical fallback chain: observed > configured weekly budget > default floor."""
    raw_budget = sum(float(rule.weekly_limit) for rule in meta.raw_budget_rules.values())
    configured_budget = round(raw_budget, 2)
    if observed_weekly_burn > 0.0:
        effective = observed_weekly_burn
    elif raw_budget > 0.0:
        effective = configured_budget
    else:
        effective = DEFAULT_WEEKLY_BURN_NZD
    target_budget = configured_budget if raw_budget > 0.0 else effective
    return effective, target_budget


def _assemble_burn_projection(
    db: Session,
    meta: _AnnualCategoryMetadata,
    totals: _VariableSpendTotals,
    *,
    as_of: date,
    start_of_week: date,
    lookback_weeks: int,
) -> BurnProjection:
    """Assemble final BurnProjection from collected variable totals, recurring, and rules."""
    recurring_totals, recurring_commitments, recurring_weekly_burn = _collect_recurring_burn(
        db, as_of, meta, totals.canonical_names
    )
    regular_weekly = sum(totals.lookback_totals.values()) / float(lookback_weeks)
    annual_weekly = sum(totals.annual_totals.values()) / WEEKS_PER_YEAR
    variable_weekly_burn = round(regular_weekly + annual_weekly, 2)
    observed_weekly_burn = round(variable_weekly_burn + recurring_weekly_burn, 2)
    effective_weekly_burn, weekly_budget_target = _resolve_effective_burn(observed_weekly_burn, meta)
    this_week_var, this_week_spend, this_week_cats = _build_this_week_breakdown(
        totals, recurring_totals
    )
    return BurnProjection(
        as_of_date=as_of, lookback_weeks=lookback_weeks, start_of_week=start_of_week,
        variable_weekly_burn=variable_weekly_burn, recurring_weekly_burn=recurring_weekly_burn,
        observed_weekly_burn=observed_weekly_burn, effective_weekly_burn=effective_weekly_burn,
        weekly_budget_target=weekly_budget_target, this_week_variable_spend=this_week_var,
        this_week_spend=this_week_spend, this_week_categories=this_week_cats,
        categories=_build_category_burn_estimates(totals, recurring_totals, meta, lookback_weeks),
        recurring_commitments=recurring_commitments,
    )


def project_burn(
    db: Session,
    *,
    lookback_weeks: int = 4,
    reference_date: date | None = None,
) -> BurnProjection:
    """Project trailing weekly burn, category burn estimates, recurring commitments, and week spend."""
    if lookback_weeks < 1:
        raise InvalidWindowError("lookback_weeks must be at least 1.")

    as_of = reference_date or date.today()
    start_of_week = as_of - timedelta(days=as_of.weekday())
    resolved = resolve_reporting_window(ReportingWindow(period="week"), reference_date=as_of)
    meta = _load_annual_metadata(db, resolved, rollup_base=False)
    totals = _collect_variable_spend(
        db,
        meta,
        lookback_start=as_of - timedelta(days=lookback_weeks * 7),
        annual_start=as_of - timedelta(days=DAYS_PER_YEAR),
        start_of_week=start_of_week,
        as_of=as_of,
    )
    return _assemble_burn_projection(
        db, meta, totals, as_of=as_of, start_of_week=start_of_week, lookback_weeks=lookback_weeks
    )
