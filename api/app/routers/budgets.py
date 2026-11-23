"""Budgets, average-burn estimation, and category spending guardrails router."""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import BudgetRule
from app.schemas import (
    AverageBurnReportResponse,
    BudgetRuleCreate,
    BudgetRuleRead,
    CategoryBurnEstimateRead,
    CategorySpendingReport,
    RecurringCommitmentRead,
    SpendingBreakdownResponse,
)
from app.services.category_taxonomy import (
    CategoryTaxonomyError,
    resolve_budget_rule_period,
    sync_budget_rule_amortization,
)
from app.services.spending_projection import (
    MIN_SIGNIFICANT_WEEKLY_BURN_NZD,
    InvalidWindowError,
    ReportingWindow,
    annual_to_weekly,
    monthly_to_weekly,
    project_burn,
    project_window,
    weekly_to_monthly,
)

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


def _save_budget_rule_record(
    db: Session,
    *,
    existing: Optional[BudgetRule],
    category: str,
    weekly_limit: float,
    monthly_limit: float,
    budget_period: str,
    is_active: bool,
) -> BudgetRule:
    """Persist a created or updated BudgetRule with canonical category casing."""
    if existing:
        existing.category = category
        existing.weekly_limit = weekly_limit
        existing.monthly_limit = monthly_limit
        existing.budget_period = budget_period
        existing.is_active = is_active
        db.commit()
        db.refresh(existing)
        return existing

    new_rule = BudgetRule(
        category=category,
        weekly_limit=weekly_limit,
        monthly_limit=monthly_limit,
        budget_period=budget_period,
        is_active=is_active,
    )
    db.add(new_rule)
    db.commit()
    db.refresh(new_rule)
    return new_rule


def _sync_budget_or_400(
    db: Session,
    *,
    category_name: str,
    is_active: bool,
    explicit_period: Optional[str] = None,
    existing_period: Optional[str] = None,
    existing_is_active: bool = True,
    annual_limit_only: bool = False,
) -> tuple[str, str]:
    """Resolve budget cadence and sync taxonomy amortization, mapping domain errors to HTTP 400."""
    try:
        resolved_period = resolve_budget_rule_period(
            db,
            category_name,
            explicit_period=explicit_period,
            existing_period=existing_period,
            existing_is_active=existing_is_active,
            annual_limit_only=annual_limit_only,
        )
        canonical_cat = sync_budget_rule_amortization(
            db,
            category_name=category_name,
            budget_period=resolved_period,
            is_active=is_active,
        )
    except CategoryTaxonomyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return canonical_cat, resolved_period


@router.post("", response_model=BudgetRuleRead, status_code=status.HTTP_200_OK)
def upsert_budget_rule(
    payload: BudgetRuleCreate,
    db: Session = Depends(get_db),
):
    """Create or update a spending target budget (weekly, monthly, or annual) for a category."""
    cleaned = payload.category.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Category name cannot be empty")
    weekly, monthly = _resolve_budget_limits(
        payload.weekly_limit, payload.monthly_limit, payload.annual_limit
    )
    existing = db.scalar(
        select(BudgetRule).where(func.lower(func.trim(BudgetRule.category)) == cleaned.lower())
    )
    annual_only = (
        payload.annual_limit is not None
        and payload.weekly_limit is None
        and payload.monthly_limit is None
    )
    canonical_cat, resolved_period = _sync_budget_or_400(
        db,
        category_name=cleaned,
        is_active=payload.is_active,
        explicit_period=payload.budget_period,
        existing_period=existing.budget_period if existing is not None else None,
        existing_is_active=bool(existing.is_active) if existing is not None else True,
        annual_limit_only=annual_only,
    )
    return _save_budget_rule_record(
        db,
        existing=existing,
        category=canonical_cat,
        weekly_limit=weekly,
        monthly_limit=monthly,
        budget_period=resolved_period,
        is_active=payload.is_active,
    )


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
        existing = db.scalar(
            select(BudgetRule).where(func.lower(func.trim(BudgetRule.category)) == cat.lower())
        )
        if existing:
            continue
        canonical_cat, resolved_period = _sync_budget_or_400(
            db, category_name=cat, is_active=True
        )
        rule = BudgetRule(
            category=canonical_cat,
            weekly_limit=weekly,
            monthly_limit=monthly,
            budget_period=resolved_period,
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
    burn = project_burn(db, lookback_weeks=lookback_weeks, reference_date=date.today())
    return AverageBurnReportResponse(
        lookback_weeks=burn.lookback_weeks,
        as_of_date=burn.as_of_date,
        variable_weekly_burn=burn.variable_weekly_burn,
        recurring_weekly_burn=burn.recurring_weekly_burn,
        effective_weekly_burn=burn.effective_weekly_burn,
        categories=[CategoryBurnEstimateRead(**vars(item)) for item in burn.categories],
        recurring_commitments=[RecurringCommitmentRead(**vars(c)) for c in burn.recurring_commitments],
    )


def _upsert_budget_rules_from_burn(db: Session, lookback_weeks: int) -> list[BudgetRule]:
    """Upsert active category BudgetRules directly from projected average weekly burn."""
    burn = project_burn(db, lookback_weeks=lookback_weeks, reference_date=date.today())
    existing_rules = {
        rule.category.strip().lower(): rule
        for rule in db.scalars(select(BudgetRule)).all()
    }
    for est in burn.categories:
        if est.average_weekly_burn < MIN_SIGNIFICANT_WEEKLY_BURN_NZD:
            continue
        canonical_cat, resolved_period = _sync_budget_or_400(
            db,
            category_name=est.category,
            is_active=True,
            explicit_period=est.budget_period or "monthly",
        )
        existing = existing_rules.get(canonical_cat.lower())
        if existing:
            existing.category = canonical_cat
            existing.weekly_limit = est.average_weekly_burn
            existing.monthly_limit = est.suggested_monthly_limit
            existing.budget_period = resolved_period
            existing.is_active = True
        else:
            db.add(
                BudgetRule(
                    category=canonical_cat,
                    weekly_limit=est.average_weekly_burn,
                    monthly_limit=est.suggested_monthly_limit,
                    budget_period=resolved_period,
                    is_active=True,
                )
            )
    db.commit()
    return list(db.scalars(select(BudgetRule).order_by(BudgetRule.category)).all())


@router.post("/sync-from-burn", response_model=list[BudgetRuleRead])
def sync_budgets_from_burn(
    lookback_weeks: int = Query(default=12, ge=1, le=52),
    db: Session = Depends(get_db),
):
    """Auto-calculate and upsert weekly/monthly BudgetRules from actual average weekly burn."""
    return _upsert_budget_rules_from_burn(db, lookback_weeks)


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
    try:
        projection = project_window(
            db,
            ReportingWindow(period=period, start_date=start_date, end_date=end_date),
            group_by=group_by,  # type: ignore[arg-type]
            reference_date=date.today(),
        )
    except InvalidWindowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return SpendingBreakdownResponse(
        period=projection.period,
        start_date=projection.start_date,
        end_date=projection.end_date,
        total_spend=projection.total_spend,
        total_income=projection.total_income,
        total_transfers=projection.total_transfers,
        net_savings=projection.net_savings,
        categories=[CategorySpendingReport(**vars(item)) for item in projection.categories],
        over_budget_count=projection.over_budget_count,
    )
