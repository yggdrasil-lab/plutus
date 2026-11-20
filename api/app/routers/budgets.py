"""Budgets, average-burn estimation, and category spending guardrails router."""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.models import BudgetRule, Category, Receipt, Transaction, TransactionSplit
from app.schemas import (
    AverageBurnReportResponse,
    BudgetRuleCreate,
    BudgetRuleRead,
    CategoryBurnEstimateRead,
    CategorySpendingReport,
    RecurringCommitmentRead,
    SpendingBreakdownResponse,
)
from app.services.burn_service import (
    MONTHS_PER_YEAR,
    TRANSFER_CATEGORIES,
    WEEKS_PER_YEAR,
    annual_to_weekly,
    calculate_annual_category_count_map,
    calculate_annual_category_spend_map,
    calculate_category_average_burn,
    calculate_weekly_burn_summary,
    expand_expense_transaction_by_receipt,
    list_recurring_commitment_details,
    monthly_to_weekly,
    sync_budgets_from_average_burn,
    to_nzd_amount,
    weekly_to_annual,
    weekly_to_monthly,
)
from app.services.category_taxonomy import (
    get_category_parent_map,
    get_descendant_category_names,
    get_parent_category,
    get_root_category,
)
from app.services.period_service import resolve_period_dates
from app.services.recurrence_service import (
    get_active_recurring_commitments,
    get_period_amortized_recurrences,
    is_recurrence_excluded_from_period,
)
from app.services.split_utils import resolve_signed_split_amount

router = APIRouter(prefix="/budgets", tags=["Budgets & Spending Awareness"])

# Standard baseline weekly budget targets for New Zealand homelabs
DEFAULT_BASELINE_BUDGETS = [
    ("Groceries", 200.0, 866.67),
    ("Food", 100.0, 433.33),
    ("Transport", 60.0, 260.00),
    ("Utilities", 80.0, 346.67),
    ("Health", 50.0, 216.67),
    ("Leisure", 50.0, 216.67),
    ("Insurance", 55.0, 238.33),
]


def _resolve_budget_limits(
    weekly: Optional[float],
    monthly: Optional[float],
    annual: Optional[float] = None,
) -> tuple[float, float]:
    """Resolve paired (weekly_limit, monthly_limit) values when any limit is provided."""
    if annual is not None:
        resolved_weekly = weekly if weekly is not None else annual_to_weekly(annual)
        resolved_monthly = monthly if monthly is not None else round(float(annual) / 12.0, 2)
        return resolved_weekly, resolved_monthly
    if weekly is None and monthly is None:
        return 100.0, weekly_to_monthly(100.0)
    if weekly is not None and monthly is None:
        return weekly, weekly_to_monthly(weekly)
    if monthly is not None and weekly is None:
        return monthly_to_weekly(monthly), monthly
    assert weekly is not None and monthly is not None
    return weekly, monthly


def _accumulate_category(
    category_map: dict[str, dict],
    category: str,
    amount_nzd: float,
    *,
    is_transfer: bool = False,
    is_amortized: bool = False,
) -> None:
    """Add an NZD amount and transaction count to a category accumulator."""
    if category not in category_map:
        category_map[category] = {
            "spend": 0.0,
            "count": 0,
            "actual_count": 0,
            "amortized_count": 0,
            "is_transfer": is_transfer,
        }
    category_map[category]["spend"] += amount_nzd
    category_map[category]["count"] += 1
    if is_amortized:
        category_map[category]["amortized_count"] += 1
    else:
        category_map[category]["actual_count"] += 1


def _build_category_report(
    category: str,
    cat_spend: float,
    count: int,
    total_spend: float,
    is_tx_transfer: bool,
    rule: Optional[BudgetRule],
    period: Optional[str],
    *,
    parent_category: Optional[str] = None,
    annual_cat_spend: Optional[float] = None,
    annual_cat_count: Optional[int] = None,
    actual_count: int = 0,
    amortized_count: int = 0,
    is_annual_category: bool = False,
) -> CategorySpendingReport:
    """Build one CategorySpendingReport with budget utilization and warning level."""
    rule_period = (rule.budget_period or "monthly").strip().lower() if rule else "monthly"
    is_annual_rule = bool(
        not is_tx_transfer and (is_annual_category or (rule is not None and rule_period == "annual"))
    )
    display_spend = (
        round(annual_cat_spend, 2)
        if (is_annual_rule and annual_cat_spend is not None)
        else cat_spend
    )
    display_count = (
        annual_cat_count
        if (is_annual_rule and annual_cat_count is not None)
        else count
    )
    display_actual_count = display_count if is_annual_rule else actual_count
    display_amortized_count = 0 if is_annual_rule else amortized_count

    pct = round((cat_spend / total_spend * 100), 1) if (total_spend > 0 and not is_tx_transfer) else 0.0
    budget_limit: Optional[float] = None
    annual_limit_val: Optional[float] = None
    percent_used: Optional[float] = None
    is_over = False
    warning = "normal"

    if rule and not is_tx_transfer:
        if is_annual_rule:
            annual_limit_val = weekly_to_annual(rule.weekly_limit)
            budget_limit = annual_limit_val
        else:
            budget_limit = float(rule.weekly_limit if period == "week" else rule.monthly_limit)
        if budget_limit > 0:
            percent_used = round((display_spend / budget_limit * 100), 1)
            is_over = display_spend > budget_limit
            if is_over:
                warning = "danger"
            elif percent_used >= 80.0:
                warning = "warning"

    resolved_period = "annual" if is_annual_rule else (rule_period if (rule and not is_tx_transfer) else "monthly")

    return CategorySpendingReport(
        category=category,
        parent_category=parent_category,
        total_spend=display_spend,
        transaction_count=display_count,
        actual_transaction_count=display_actual_count,
        amortized_count=display_amortized_count,
        percentage=pct,
        budget_limit=budget_limit,
        percent_used=percent_used,
        is_over_budget=is_over,
        warning_level=warning,
        is_transfer=is_tx_transfer,
        budget_period=resolved_period,
        annual_limit=annual_limit_val,
    )


@router.get("", response_model=list[BudgetRuleRead])
def list_budget_rules(
    is_active: Optional[bool] = Query(default=True),
    db: Session = Depends(get_db),
):
    """List all configured category budget rules."""
    stmt = select(BudgetRule)
    if is_active is not None:
        stmt = stmt.where(BudgetRule.is_active == is_active)
    return db.scalars(stmt.order_by(BudgetRule.category)).all()


@router.post("", response_model=BudgetRuleRead, status_code=status.HTTP_200_OK)
def upsert_budget_rule(
    payload: BudgetRuleCreate,
    db: Session = Depends(get_db),
):
    """Create or update a spending target budget (weekly, monthly, or annual) for a category."""
    cat = payload.category.strip()
    if not cat:
        raise HTTPException(status_code=400, detail="Category name cannot be empty")

    weekly, monthly = _resolve_budget_limits(
        payload.weekly_limit, payload.monthly_limit, payload.annual_limit
    )
    existing = db.scalar(select(BudgetRule).where(func.lower(BudgetRule.category) == cat.lower()))
    cat_row = db.scalar(select(Category).where(func.lower(Category.name) == cat.lower()))
    if payload.budget_period is not None:
        resolved_period = payload.budget_period
    elif existing is not None:
        resolved_period = existing.budget_period
    elif (
        payload.annual_limit is not None
        and payload.weekly_limit is None
        and payload.monthly_limit is None
    ):
        resolved_period = "annual"
    elif cat_row is not None and (cat_row.amortize_period or "").strip().lower() == "annual":
        resolved_period = "annual"
    else:
        resolved_period = "monthly"

    if cat_row is not None and not cat_row.is_system:
        cat_row.amortize_period = "annual" if resolved_period == "annual" else None

    if existing:
        existing.weekly_limit = weekly
        existing.monthly_limit = monthly
        existing.budget_period = resolved_period
        existing.is_active = payload.is_active
        db.commit()
        db.refresh(existing)
        return existing

    new_rule = BudgetRule(
        category=cat,
        weekly_limit=weekly,
        monthly_limit=monthly,
        budget_period=resolved_period,
        is_active=payload.is_active,
    )
    db.add(new_rule)
    db.commit()
    db.refresh(new_rule)
    return new_rule


@router.delete("/{budget_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_budget_rule(
    budget_id: int,
    db: Session = Depends(get_db),
):
    """Delete an existing category budget rule."""
    rule = db.get(BudgetRule, budget_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Budget rule not found")
    db.delete(rule)
    db.commit()
    return None


@router.post("/init-defaults", response_model=list[BudgetRuleRead])
def initialize_default_budgets(
    db: Session = Depends(get_db),
):
    """Initialize default sensible baseline category budgets if none are configured."""
    created = []
    for cat, weekly, monthly in DEFAULT_BASELINE_BUDGETS:
        existing = db.scalar(select(BudgetRule).where(func.lower(BudgetRule.category) == cat.lower()))
        if not existing:
            rule = BudgetRule(
                category=cat,
                weekly_limit=weekly,
                monthly_limit=monthly,
                is_active=True,
            )
            db.add(rule)
            created.append(rule)
    if created:
        db.commit()
        for rule in created:
            db.refresh(rule)
    return db.scalars(select(BudgetRule).order_by(BudgetRule.category)).all()


@router.get("/average-burn", response_model=AverageBurnReportResponse)
def get_average_burn_report(
    lookback_weeks: int = Query(default=12, ge=1, le=52),
    db: Session = Depends(get_db),
):
    """Compute average weekly burn across categories from trailing variable spend and recurring items."""
    today = date.today()
    summary = calculate_weekly_burn_summary(db, today, lookback_weeks=lookback_weeks)
    estimates = calculate_category_average_burn(db, today, lookback_weeks=lookback_weeks)
    commitments = list_recurring_commitment_details(db, today)
    return AverageBurnReportResponse(
        lookback_weeks=lookback_weeks,
        as_of_date=today,
        variable_weekly_burn=summary.variable_weekly_burn,
        recurring_weekly_burn=summary.recurring_weekly_burn,
        effective_weekly_burn=summary.effective_weekly_burn,
        categories=[
            CategoryBurnEstimateRead(
                category=item.category,
                parent_category=item.parent_category,
                variable_weekly_burn=item.variable_weekly_burn,
                recurring_weekly_burn=item.recurring_weekly_burn,
                average_weekly_burn=item.average_weekly_burn,
                suggested_monthly_limit=item.suggested_monthly_limit,
                suggested_annual_limit=item.suggested_annual_limit,
                budget_period=item.budget_period,
                variable_percentage=item.variable_percentage,
                total_percentage=item.total_percentage,
            )
            for item in estimates
        ],
        recurring_commitments=[RecurringCommitmentRead(**vars(c)) for c in commitments],
    )


@router.post("/sync-from-burn", response_model=list[BudgetRuleRead])
def sync_budgets_from_burn(
    lookback_weeks: int = Query(default=12, ge=1, le=52),
    db: Session = Depends(get_db),
):
    """Auto-calculate and upsert weekly/monthly BudgetRules from actual average weekly burn."""
    return sync_budgets_from_average_burn(db, date.today(), lookback_weeks=lookback_weeks)


def _rollup_budget_rules_to_base(
    raw_budget_rules: dict[str, BudgetRule],
    parent_map: dict[str, str],
    canonical_name_map: dict[str, str],
    annual_base_roots: set[str],
) -> dict[str, BudgetRule]:
    """Aggregate child and root BudgetRules under each base class for group_by=base."""
    grouped_rules: dict[str, list[BudgetRule]] = {}
    grouped_names: dict[str, str] = {}
    for rule in raw_budget_rules.values():
        root_name = get_root_category(rule.category, parent_map)
        root_lower = root_name.lower()
        grouped_names[root_lower] = canonical_name_map.get(root_lower, root_name)
        grouped_rules.setdefault(root_lower, []).append(rule)

    rolled: dict[str, BudgetRule] = {}
    for root_lower, rules_list in grouped_rules.items():
        root_rule = next(
            (r for r in rules_list if r.category.strip().lower() == root_lower),
            None,
        )
        sum_weekly = round(sum(float(r.weekly_limit) for r in rules_list), 2)
        sum_monthly = round(sum(float(r.monthly_limit) for r in rules_list), 2)
        if root_lower in annual_base_roots:
            resolved_rule_period = "annual"
        elif root_rule is not None:
            resolved_rule_period = (root_rule.budget_period or "monthly").strip().lower()
        else:
            child_periods = {(r.budget_period or "monthly").strip().lower() for r in rules_list}
            resolved_rule_period = next(iter(child_periods)) if len(child_periods) == 1 else "monthly"

        rolled[root_lower] = BudgetRule(
            category=grouped_names[root_lower],
            weekly_limit=sum_weekly,
            monthly_limit=sum_monthly,
            budget_period=resolved_rule_period,
            is_active=True,
        )
    return rolled


@router.get("/breakdown", response_model=SpendingBreakdownResponse)
def get_spending_breakdown(
    period: Optional[str] = Query(
        default="month",
        description="'week', 'last_week', 'month', 'last_month', or 'all' (omit when passing start_date/end_date)",
    ),
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    group_by: str = Query(
        default="leaf",
        description="'leaf' for granular subclasses or 'base' for base-class inheritance rollup",
    ),
    db: Session = Depends(get_db),
):
    """Retrieve detailed category spending breakdown, budget utilization, and overspending warnings."""
    if group_by not in ("leaf", "base"):
        raise HTTPException(status_code=400, detail="group_by must be 'leaf' or 'base'")
    rollup_base = group_by == "base"
    today = date.today()
    effective_period = None if (start_date is not None or end_date is not None) else period
    start_date, end_date = resolve_period_dates(effective_period, start_date, end_date, today=today)
    ref_date = end_date or today

    parent_map = get_category_parent_map(db)
    raw_budget_rules = {
        r.category.lower(): r
        for r in db.scalars(select(BudgetRule).where(BudgetRule.is_active == True)).all()
    }
    annual_cats = {
        cat_lower
        for cat_lower, rule in raw_budget_rules.items()
        if (rule.budget_period or "monthly").strip().lower() == "annual"
    }
    canonical_name_map: dict[str, str] = {
        r.category.strip().lower(): r.category.strip()
        for r in raw_budget_rules.values()
        if r.category and r.category.strip()
    }
    for db_cat in db.scalars(select(Category)).all():
        if not db_cat.name or not db_cat.name.strip():
            continue
        cat_lower = db_cat.name.strip().lower()
        canonical_name_map.setdefault(cat_lower, db_cat.name.strip())
        if db_cat.is_active and (db_cat.amortize_period or "").strip().lower() == "annual":
            annual_cats.add(cat_lower)
    for child_name, parent_name in parent_map.items():
        canonical_name_map.setdefault(child_name.lower(), child_name)
        canonical_name_map.setdefault(parent_name.lower(), parent_name)

    annual_base_roots = {
        c
        for c in annual_cats
        if get_parent_category(canonical_name_map.get(c, c), parent_map) is None
    }
    fetch_annual_cats = set(annual_cats)
    if rollup_base:
        for root_lower in annual_base_roots:
            fetch_annual_cats.update(get_descendant_category_names(root_lower, parent_map))

    raw_annual_spend = calculate_annual_category_spend_map(db, ref_date, fetch_annual_cats)
    raw_annual_count = calculate_annual_category_count_map(db, ref_date, fetch_annual_cats)
    if rollup_base:
        annual_spend_map: dict[str, float] = {}
        annual_count_map: dict[str, int] = {}
        for raw_cat_lower, spend_val in raw_annual_spend.items():
            root_lower = get_root_category(
                canonical_name_map.get(raw_cat_lower, raw_cat_lower), parent_map
            ).lower()
            target_key = root_lower if root_lower in annual_base_roots else raw_cat_lower
            annual_spend_map[target_key] = annual_spend_map.get(target_key, 0.0) + spend_val
        for raw_cat_lower, cnt_val in raw_annual_count.items():
            root_lower = get_root_category(
                canonical_name_map.get(raw_cat_lower, raw_cat_lower), parent_map
            ).lower()
            target_key = root_lower if root_lower in annual_base_roots else raw_cat_lower
            annual_count_map[target_key] = annual_count_map.get(target_key, 0) + cnt_val
        effective_budget_rules = _rollup_budget_rules_to_base(
            raw_budget_rules, parent_map, canonical_name_map, annual_base_roots
        )
        effective_annual_set = annual_base_roots
    else:
        annual_spend_map = raw_annual_spend
        annual_count_map = raw_annual_count
        effective_budget_rules = raw_budget_rules
        effective_annual_set = annual_cats

    is_weekly_period = effective_period in ("week", "last_week")
    is_monthly_period = effective_period in ("month", "last_month")
    amortized_recurrences = get_period_amortized_recurrences(effective_period)

    stmt = select(Transaction).options(
        joinedload(Transaction.receipt).joinedload(Receipt.items),
        joinedload(Transaction.loan),
        joinedload(Transaction.splits).joinedload(TransactionSplit.loan),
    )
    if start_date:
        stmt = stmt.where(Transaction.transaction_date >= start_date)
    if end_date:
        stmt = stmt.where(Transaction.transaction_date <= end_date)
    txs = db.scalars(stmt).unique().all()

    total_spend = 0.0
    total_income = 0.0
    total_transfers = 0.0
    category_spend_map: dict[str, dict] = {}

    for tx in txs:
        amt = float(tx.amount)
        cat = tx.category or "General"
        cat_lower = cat.lower()
        amt_nzd = to_nzd_amount(amt, tx.currency, tx.exchange_rate)
        splits = getattr(tx, "splits", None) or []

        if splits:
            recurrence_excluded = is_recurrence_excluded_from_period(effective_period, tx.recurrence)
            for s in splits:
                signed_amt = resolve_signed_split_amount(s, splits, amt)
                s_cat = (s.category or cat).strip() or "General"
                s_lower = s_cat.lower()
                s_nzd = round(
                    to_nzd_amount(abs(signed_amt), tx.currency, tx.exchange_rate),
                    2,
                )
                if s_lower in TRANSFER_CATEGORIES:
                    total_transfers += s_nzd
                    _accumulate_category(category_spend_map, "Transfers", s_nzd, is_transfer=True)
                    continue
                if s.loan is not None and (s.loan.loan_type or "").strip().lower() == "lent":
                    continue
                if signed_amt < 0:
                    if recurrence_excluded:
                        continue
                    effective_cat = get_root_category(s_cat, parent_map) if rollup_base else s_cat
                    effective_lower = effective_cat.lower()
                    if (is_weekly_period or is_monthly_period) and (
                        s_lower in annual_cats
                        or (rollup_base and effective_lower in annual_base_roots)
                    ):
                        continue
                    total_spend += s_nzd
                    _accumulate_category(category_spend_map, effective_cat, s_nzd)
                elif signed_amt > 0:
                    total_income += s_nzd
            continue

        is_lent_loan = (
            tx.loan is not None and (tx.loan.loan_type or "").strip().lower() == "lent"
        )

        if cat_lower in TRANSFER_CATEGORIES:
            total_transfers += amt_nzd
            _accumulate_category(category_spend_map, "Transfers", amt_nzd, is_transfer=True)
            continue

        if amt < 0:
            if is_lent_loan:
                continue
            if is_recurrence_excluded_from_period(effective_period, tx.recurrence):
                continue

            for item_cat, item_nzd in expand_expense_transaction_by_receipt(tx):
                effective_cat = get_root_category(item_cat, parent_map) if rollup_base else item_cat
                raw_lower = item_cat.lower()
                effective_lower = effective_cat.lower()
                if (is_weekly_period or is_monthly_period) and (
                    raw_lower in annual_cats
                    or (rollup_base and effective_lower in annual_base_roots)
                ):
                    continue
                total_spend += item_nzd
                _accumulate_category(category_spend_map, effective_cat, item_nzd)
        elif amt > 0:
            if is_lent_loan:
                if tx.loan_payment_amount is not None:
                    tx_currency = (tx.currency or "NZD").strip().upper()
                    repayment_nzd = (
                        to_nzd_amount(float(tx.loan_payment_amount), tx.currency, tx.exchange_rate)
                        if tx_currency != "NZD"
                        else float(tx.loan_payment_amount)
                    )
                else:
                    repayment_nzd = amt_nzd
                total_income += max(0.0, amt_nzd - repayment_nzd)
            else:
                total_income += amt_nzd

    if amortized_recurrences:
        for rec_tx, weekly_nzd in get_active_recurring_commitments(db, ref_date):
            rec = (rec_tx.recurrence or "").strip().lower()
            if rec not in amortized_recurrences:
                continue
            cat = rec_tx.category or "General"
            if rollup_base:
                cat = get_root_category(cat, parent_map)
            amortized_nzd = weekly_to_monthly(weekly_nzd) if is_monthly_period else weekly_nzd
            total_spend += amortized_nzd
            _accumulate_category(category_spend_map, cat, amortized_nzd, is_amortized=True)
        divisor = MONTHS_PER_YEAR if is_monthly_period else WEEKS_PER_YEAR
        if rollup_base:
            annual_keys_to_emit = sorted(
                annual_base_roots
                | {
                    c
                    for c in annual_cats
                    if get_root_category(canonical_name_map.get(c, c), parent_map).lower()
                    not in annual_base_roots
                }
            )
        else:
            annual_keys_to_emit = sorted(annual_cats)

        for cat_lower in annual_keys_to_emit:
            annual_total_nzd = annual_spend_map.get(cat_lower, 0.0)
            if annual_total_nzd > 0:
                raw_display = canonical_name_map.get(cat_lower, cat_lower.title())
                target_display = (
                    get_root_category(raw_display, parent_map) if rollup_base else raw_display
                )
                period_equiv = round(annual_total_nzd / divisor, 2)
                total_spend += period_equiv
                _accumulate_category(category_spend_map, target_display, period_equiv)

    total_spend = round(total_spend, 2)
    total_income = round(total_income, 2)
    total_transfers = round(total_transfers, 2)
    net_savings = round(total_income - total_spend, 2)

    reports: list[CategorySpendingReport] = []
    processed_lower_cats: set[str] = set()

    for cat, data_dict in category_spend_map.items():
        cat_spend = round(data_dict["spend"], 2)
        cat_lower = cat.lower()
        is_tx_transfer = data_dict.get("is_transfer", False)
        processed_lower_cats.add(cat_lower)
        reports.append(
            _build_category_report(
                category=cat,
                parent_category=None if rollup_base else get_parent_category(cat, parent_map),
                cat_spend=cat_spend,
                count=data_dict["count"],
                total_spend=total_spend,
                is_tx_transfer=is_tx_transfer,
                rule=effective_budget_rules.get(cat_lower),
                period=period,
                annual_cat_spend=annual_spend_map.get(cat_lower),
                annual_cat_count=annual_count_map.get(cat_lower, 0),
                actual_count=data_dict["actual_count"],
                amortized_count=data_dict["amortized_count"],
                is_annual_category=(cat_lower in effective_annual_set and not is_tx_transfer),
            )
        )

    for cat_lower, rule in effective_budget_rules.items():
        if cat_lower not in processed_lower_cats:
            reports.append(
                _build_category_report(
                    category=rule.category,
                    parent_category=(
                        None if rollup_base else get_parent_category(rule.category, parent_map)
                    ),
                    cat_spend=0.0,
                    count=0,
                    total_spend=total_spend,
                    is_tx_transfer=False,
                    rule=rule,
                    period=period,
                    annual_cat_spend=annual_spend_map.get(cat_lower, 0.0),
                    annual_cat_count=annual_count_map.get(cat_lower, 0),
                    actual_count=0,
                    amortized_count=0,
                    is_annual_category=(cat_lower in effective_annual_set),
                )
            )

    reports.sort(key=lambda r: (r.is_transfer, -r.total_spend, r.category))
    over_budget_count = sum(1 for r in reports if r.is_over_budget and not r.is_transfer)

    return SpendingBreakdownResponse(
        period=period or "custom",
        start_date=start_date,
        end_date=end_date,
        total_spend=total_spend,
        total_income=total_income,
        total_transfers=total_transfers,
        net_savings=net_savings,
        categories=reports,
        over_budget_count=over_budget_count,
    )
