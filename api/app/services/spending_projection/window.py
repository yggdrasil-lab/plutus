"""Window-level spending, income, transfer, budget, and tag projection."""

from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from app.models import BudgetRule, Transaction
from app.services.category_taxonomy import get_parent_category, get_root_category
from app.services.fx_service import to_nzd_amount
from app.services.spending_projection._lines import (
    _is_lent_loan,
    _load_budget_context,
    _resolve_unsplit_inflow_nzd,
    expand_expense_buckets,
    expand_expense_lines,
    get_active_recurring_commitment_allocations,
)
from app.services.spending_projection._types import (
    MONTHS_PER_YEAR,
    TRANSFER_CATEGORIES,
    WEEKS_PER_YEAR,
    CategoryProjection,
    GroupByLiteral,
    InvalidWindowError,
    ReportingWindow,
    ResolvedWindow,
    WindowProjection,
    _BudgetProjectionContext,
    _CategoryBucket,
    _TagAccumulator,
    _WindowAccumulator,
)
from app.services.spending_projection._window import (
    _query_window_transactions,
    is_recurrence_excluded_from_period,
    resolve_reporting_window,
    weekly_to_annual,
)


def _accumulate_split_transaction(
    tx: Transaction,
    ctx: _BudgetProjectionContext,
    acc: _WindowAccumulator,
) -> None:
    """Accumulate a manual-split transaction into transfers, income, and category spend."""
    cat = (tx.category or "General").strip() or "General"
    recurrence_excluded = is_recurrence_excluded_from_period(
        ctx.resolved.effective_period, tx.recurrence
    )
    for s in getattr(tx, "splits", None) or []:
        signed_amt = s.signed_amount()
        s_cat = (s.category or cat).strip() or "General"
        s_lower = s_cat.lower()
        s_nzd = round(to_nzd_amount(abs(signed_amt), tx.currency, tx.exchange_rate), 2)
        if s_lower in TRANSFER_CATEGORIES:
            acc.total_transfers += s_nzd
            acc.add_category("Transfers", s_nzd, is_transfer=True)
            continue
        if _is_lent_loan(getattr(s, "loan", None)):
            continue
        if signed_amt < 0 and not recurrence_excluded:
            effective_cat = ctx.resolve_category(s_cat)
            if not ctx.is_smoothed_annual(s_lower, effective_cat.lower()):
                acc.total_spend += s_nzd
                acc.add_category(effective_cat, s_nzd)
        elif signed_amt > 0:
            acc.total_income += s_nzd


def _accumulate_unsplit_transaction(
    tx: Transaction,
    ctx: _BudgetProjectionContext,
    acc: _WindowAccumulator,
) -> None:
    """Accumulate an unsplit transaction into transfers, income, or receipt-expanded spend."""
    amt = float(tx.amount)
    cat = (tx.category or "General").strip() or "General"
    amt_nzd = to_nzd_amount(amt, tx.currency, tx.exchange_rate)
    if cat.lower() in TRANSFER_CATEGORIES:
        acc.total_transfers += amt_nzd
        acc.add_category("Transfers", amt_nzd, is_transfer=True)
        return

    is_lent = _is_lent_loan(getattr(tx, "loan", None))
    if amt < 0:
        if is_lent or is_recurrence_excluded_from_period(
            ctx.resolved.effective_period, tx.recurrence
        ):
            return
        for item_cat, item_nzd in expand_expense_buckets(tx):
            effective_cat = ctx.resolve_category(item_cat)
            if not ctx.is_smoothed_annual(item_cat.lower(), effective_cat.lower()):
                acc.total_spend += item_nzd
                acc.add_category(effective_cat, item_nzd)
    elif amt > 0:
        acc.total_income += _resolve_unsplit_inflow_nzd(tx, amt_nzd, is_lent=is_lent)


def _accumulate_window_transaction(
    tx: Transaction,
    ctx: _BudgetProjectionContext,
    acc: _WindowAccumulator,
    tag_acc: _TagAccumulator,
) -> None:
    """Accumulate one transaction's window totals and canonical pre-smoothing tag totals."""
    for line in expand_expense_lines(tx):
        if not line.effective_tags:
            continue
        tag_acc.add_line(
            tx_id=tx.id,
            tx_date=tx.transaction_date,
            category=ctx.resolve_category(line.category),
            amount_nzd=line.amount_nzd,
            tags=line.effective_tags,
        )
    if getattr(tx, "splits", None):
        _accumulate_split_transaction(tx, ctx, acc)
    else:
        _accumulate_unsplit_transaction(tx, ctx, acc)


def _inject_amortized_recurrences(
    db: Session,
    ctx: _BudgetProjectionContext,
    acc: _WindowAccumulator,
) -> None:
    """Inject amortized recurring commitments into period category spend for weekly/monthly windows."""
    if not ctx.resolved.amortized_recurrences:
        return
    for alloc in get_active_recurring_commitment_allocations(db, ctx.resolved.as_of_date):
        if alloc.recurrence not in ctx.resolved.amortized_recurrences:
            continue
        target_cat = ctx.resolve_category(alloc.category)
        if ctx.is_smoothed_annual(alloc.category.lower(), target_cat.lower()):
            continue
        amortized_nzd = (
            alloc.monthly_nzd if ctx.resolved.is_monthly_cadence else alloc.weekly_nzd
        )
        acc.total_spend += amortized_nzd
        acc.add_category(target_cat, amortized_nzd, is_amortized=True)


def _inject_annual_category_smoothing(
    ctx: _BudgetProjectionContext,
    acc: _WindowAccumulator,
) -> None:
    """Inject 365-day smoothed weekly/monthly equivalents for Annualized Categories."""
    if not ctx.resolved.is_budget_cadence_window:
        return
    divisor = MONTHS_PER_YEAR if ctx.resolved.is_monthly_cadence else WEEKS_PER_YEAR
    if ctx.rollup_base:
        annual_keys = sorted(
            set(ctx.annual_base_roots)
            | {
                c
                for c in ctx.annual_cats
                if get_root_category(ctx.canonical_name_map.get(c) or c, ctx.parent_map).lower()
                not in ctx.annual_base_roots
            }
        )
    else:
        annual_keys = sorted(ctx.annual_cats)

    for cat_lower in annual_keys:
        annual_total_nzd = ctx.annual_spend_map.get(cat_lower, 0.0)
        if annual_total_nzd <= 0:
            continue
        raw_display = ctx.canonical_name_map.get(cat_lower) or cat_lower.title()
        target_display = ctx.resolve_category(raw_display)
        period_equiv = round(annual_total_nzd / divisor, 2)
        acc.total_spend += period_equiv
        acc.add_category(target_display, period_equiv, is_amortized=True)


def _evaluate_budget_guardrail(
    display_spend: float,
    rule: Optional[BudgetRule],
    resolved: ResolvedWindow,
    *,
    is_annual_rule: bool,
    is_transfer: bool,
) -> tuple[Optional[float], Optional[float], Optional[float], bool, str]:
    """Compute (budget_limit, annual_limit, percent_used, is_over_budget, warning_level)."""
    if not rule or is_transfer:
        return None, None, None, False, "normal"
    if is_annual_rule:
        annual_limit_val: Optional[float] = weekly_to_annual(rule.weekly_limit)
        budget_limit: Optional[float] = annual_limit_val
    else:
        annual_limit_val = None
        budget_limit = float(
            rule.weekly_limit if resolved.is_weekly_cadence else rule.monthly_limit
        )
    if not budget_limit or budget_limit <= 0:
        return budget_limit, annual_limit_val, None, False, "normal"
    percent_used = round((display_spend / budget_limit * 100), 1)
    is_over = display_spend > budget_limit
    warning = "danger" if is_over else ("warning" if percent_used >= 80.0 else "normal")
    return budget_limit, annual_limit_val, percent_used, is_over, warning


def _resolve_display_metrics(
    cat_lower: str,
    bucket: _CategoryBucket,
    ctx: _BudgetProjectionContext,
    *,
    use_smoothing: bool,
) -> tuple[float, int, int, int]:
    """Compute (display_spend, display_count, display_actual, display_amortized) for a bucket."""
    cat_spend = round(bucket.spend, 2)
    annual_spend = ctx.annual_spend_map.get(cat_lower)
    annual_count = ctx.annual_count_map.get(cat_lower, 0)
    if not use_smoothing:
        return cat_spend, bucket.count, bucket.actual_count, bucket.amortized_count
    display_spend = (
        round(annual_spend, 2)
        if annual_spend is not None
        else (0.0 if bucket.count == 0 else cat_spend)
    )
    return display_spend, annual_count, annual_count, 0


def _build_category_projection(
    category: str,
    bucket: _CategoryBucket,
    total_spend: float,
    ctx: _BudgetProjectionContext,
) -> CategoryProjection:
    """Build an immutable CategoryProjection with budget cadence utilization and warnings."""
    cat_lower = category.lower()
    rule = ctx.effective_budget_rules.get(cat_lower)
    rule_period = (rule.budget_period or "monthly").strip().lower() if rule else "monthly"
    is_annual_rule = bool(
        not bucket.is_transfer
        and (cat_lower in ctx.effective_annual_set or (rule is not None and rule_period == "annual"))
    )
    use_smoothing = is_annual_rule and ctx.resolved.is_budget_cadence_window
    display_spend, display_count, display_actual, display_amortized = _resolve_display_metrics(
        cat_lower, bucket, ctx, use_smoothing=use_smoothing
    )
    cat_spend = round(bucket.spend, 2)
    pct = round((cat_spend / total_spend * 100), 1) if (total_spend > 0 and not bucket.is_transfer) else 0.0
    budget_limit, annual_limit, percent_used, is_over, warning = _evaluate_budget_guardrail(
        display_spend, rule, ctx.resolved, is_annual_rule=is_annual_rule, is_transfer=bucket.is_transfer
    )
    resolved_period = "annual" if is_annual_rule else (rule_period if (rule and not bucket.is_transfer) else "monthly")
    parent_cat = None if ctx.rollup_base else get_parent_category(category, ctx.parent_map)
    return CategoryProjection(
        category=category, parent_category=parent_cat, total_spend=display_spend,
        transaction_count=display_count, actual_transaction_count=display_actual,
        amortized_count=display_amortized, percentage=pct, budget_limit=budget_limit,
        percent_used=percent_used, is_over_budget=is_over, warning_level=warning,
        is_transfer=bucket.is_transfer, budget_period=resolved_period, annual_limit=annual_limit,
    )


def _build_all_category_projections(
    acc: _WindowAccumulator,
    total_spend: float,
    ctx: _BudgetProjectionContext,
) -> tuple[CategoryProjection, ...]:
    """Build sorted CategoryProjection rows across active spend buckets and zero-spend BudgetRules."""
    reports: list[CategoryProjection] = []
    processed_lower: set[str] = set()
    for cat, bucket in acc.categories.items():
        processed_lower.add(cat.lower())
        reports.append(_build_category_projection(cat, bucket, total_spend, ctx))
    for cat_lower, rule in ctx.effective_budget_rules.items():
        if cat_lower not in processed_lower:
            reports.append(
                _build_category_projection(rule.category, _CategoryBucket(), total_spend, ctx)
            )
    reports.sort(key=lambda r: (r.is_transfer, -r.total_spend, r.category))
    return tuple(reports)


def project_window(
    db: Session,
    window: ReportingWindow,
    *,
    group_by: GroupByLiteral = "leaf",
    reference_date: date | None = None,
) -> WindowProjection:
    """Project spending, income, transfers, category guardrails, and tag summaries for a window."""
    if group_by not in ("leaf", "base"):
        raise InvalidWindowError("group_by must be 'leaf' or 'base'")
    resolved = resolve_reporting_window(window, reference_date=reference_date)
    ctx = _load_budget_context(db, resolved, rollup_base=(group_by == "base"))
    acc = _WindowAccumulator()
    tag_acc = _TagAccumulator()
    for tx in _query_window_transactions(db, resolved):
        _accumulate_window_transaction(tx, ctx, acc, tag_acc)
    _inject_amortized_recurrences(db, ctx, acc)
    _inject_annual_category_smoothing(ctx, acc)

    total_spend = round(acc.total_spend, 2)
    total_income = round(acc.total_income, 2)
    total_transfers = round(acc.total_transfers, 2)
    categories = _build_all_category_projections(acc, total_spend, ctx)
    tags, total_tagged_spend = tag_acc.build_projections()
    over_budget_count = sum(1 for r in categories if r.is_over_budget and not r.is_transfer)
    return WindowProjection(
        period=resolved.period_label,
        start_date=resolved.start_date,
        end_date=resolved.end_date,
        total_spend=total_spend,
        total_income=total_income,
        total_transfers=total_transfers,
        net_savings=round(total_income - total_spend, 2),
        categories=categories,
        over_budget_count=over_budget_count,
        tags=tags,
        total_tagged_spend=total_tagged_spend,
    )
