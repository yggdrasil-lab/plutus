"""Budgets, average-burn estimation, and category spending guardrails router."""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import BudgetRule, Transaction
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
    TRANSFER_CATEGORIES,
    WEEKS_PER_YEAR,
    annual_to_weekly,
    calculate_annual_category_spend_map,
    calculate_category_average_burn,
    calculate_weekly_burn_summary,
    list_recurring_commitment_details,
    monthly_to_weekly,
    sync_budgets_from_average_burn,
    to_nzd_amount,
    weekly_to_annual,
    weekly_to_monthly,
)
from app.services.period_service import resolve_period_dates
from app.services.recurrence_service import get_active_recurring_commitments

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
) -> None:
    """Add an NZD amount and transaction count to a category accumulator."""
    if category not in category_map:
        category_map[category] = {"spend": 0.0, "count": 0, "is_transfer": is_transfer}
    category_map[category]["spend"] += amount_nzd
    category_map[category]["count"] += 1


def _build_category_report(
    category: str,
    cat_spend: float,
    count: int,
    total_spend: float,
    is_tx_transfer: bool,
    rule: Optional[BudgetRule],
    period: Optional[str],
    *,
    annual_cat_spend: Optional[float] = None,
) -> CategorySpendingReport:
    """Build one CategorySpendingReport with budget utilization and warning level."""
    rule_period = (rule.budget_period or "monthly").strip().lower() if rule else "monthly"
    is_annual_rule = bool(rule and not is_tx_transfer and rule_period == "annual")
    display_spend = (
        round(annual_cat_spend, 2)
        if (is_annual_rule and annual_cat_spend is not None)
        else cat_spend
    )

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

    return CategorySpendingReport(
        category=category,
        total_spend=display_spend,
        transaction_count=count,
        percentage=pct,
        budget_limit=budget_limit,
        percent_used=percent_used,
        is_over_budget=is_over,
        warning_level=warning,
        is_transfer=is_tx_transfer,
        budget_period=rule_period if (rule and not is_tx_transfer) else "monthly",
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
    else:
        resolved_period = "monthly"

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


@router.get("/breakdown", response_model=SpendingBreakdownResponse)
def get_spending_breakdown(
    period: Optional[str] = Query(
        default="month",
        description="'week', 'last_week', 'month', 'last_month', or 'all' (omit when passing start_date/end_date)",
    ),
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    db: Session = Depends(get_db),
):
    """Retrieve detailed category spending breakdown, budget utilization, and overspending warnings."""
    today = date.today()
    effective_period = None if (start_date is not None or end_date is not None) else period
    start_date, end_date = resolve_period_dates(effective_period, start_date, end_date, today=today)

    budget_rules = {
        r.category.lower(): r
        for r in db.scalars(select(BudgetRule).where(BudgetRule.is_active == True)).all()
    }
    annual_cats = {
        cat_lower
        for cat_lower, rule in budget_rules.items()
        if (rule.budget_period or "monthly").strip().lower() == "annual"
    }
    annual_spend_map = calculate_annual_category_spend_map(db, today, annual_cats)

    stmt = select(Transaction)
    if start_date:
        stmt = stmt.where(Transaction.transaction_date >= start_date)
    if end_date:
        stmt = stmt.where(Transaction.transaction_date <= end_date)
    txs = db.scalars(stmt).all()

    total_spend = 0.0
    total_income = 0.0
    total_transfers = 0.0
    category_spend_map: dict[str, dict] = {}

    for tx in txs:
        amt = float(tx.amount)
        cat = tx.category or "General"
        cat_lower = cat.lower()
        amt_nzd = to_nzd_amount(amt, tx.currency, tx.exchange_rate)

        if cat_lower in TRANSFER_CATEGORIES:
            total_transfers += amt_nzd
            _accumulate_category(category_spend_map, "Transfers", amt_nzd, is_transfer=True)
            continue

        if amt < 0:
            if period == "week" and (tx.recurrence is not None or cat_lower in annual_cats):
                continue
            total_spend += amt_nzd
            _accumulate_category(category_spend_map, cat, amt_nzd)
        elif amt > 0:
            total_income += amt_nzd

    if period == "week":
        for rec_tx, weekly_nzd in get_active_recurring_commitments(db, today):
            cat = rec_tx.category or "General"
            total_spend += weekly_nzd
            _accumulate_category(category_spend_map, cat, weekly_nzd)
        for cat_lower in annual_cats:
            annual_total_nzd = annual_spend_map.get(cat_lower, 0.0)
            if annual_total_nzd > 0:
                rule = budget_rules[cat_lower]
                weekly_equiv = round(annual_total_nzd / WEEKS_PER_YEAR, 2)
                total_spend += weekly_equiv
                _accumulate_category(category_spend_map, rule.category, weekly_equiv)

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
                cat_spend=cat_spend,
                count=data_dict["count"],
                total_spend=total_spend,
                is_tx_transfer=is_tx_transfer,
                rule=budget_rules.get(cat_lower),
                period=period,
                annual_cat_spend=annual_spend_map.get(cat_lower),
            )
        )

    for cat_lower, rule in budget_rules.items():
        if cat_lower not in processed_lower_cats:
            reports.append(
                _build_category_report(
                    category=rule.category,
                    cat_spend=0.0,
                    count=0,
                    total_spend=total_spend,
                    is_tx_transfer=False,
                    rule=rule,
                    period=period,
                    annual_cat_spend=annual_spend_map.get(cat_lower, 0.0),
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
