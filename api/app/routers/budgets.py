"""Budgets, average-burn estimation, and category spending guardrails router."""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import (
    AverageBurnReportResponse,
    BudgetRuleCreate,
    BudgetRuleRead,
    CategoryBurnEstimateRead,
    CategorySpendingReport,
    RecurringCommitmentRead,
    SpendingBreakdownResponse,
)
from app.services.budget_rules import (
    BudgetRuleNotFoundError,
    BudgetRuleSnapshot,
    BudgetRuleUpsertInput,
    BudgetRuleValidationError,
    delete_budget_rule as delete_budget_rule_service,
    initialize_default_budgets as initialize_default_budgets_service,
    list_budget_rules as list_budget_rules_service,
    sync_budget_rules_from_burn as sync_budget_rules_from_burn_service,
    upsert_budget_rule as upsert_budget_rule_service,
)
from app.services.spending_projection import (
    InvalidWindowError,
    ReportingWindow,
    project_burn,
    project_window,
)

router = APIRouter(prefix="/budgets", tags=["Budgets & Spending Awareness"])


def _to_read_model(snapshot: BudgetRuleSnapshot) -> BudgetRuleRead:
    return BudgetRuleRead(**vars(snapshot))


@router.get("", response_model=list[BudgetRuleRead])
def list_budget_rules(
    is_active: Optional[bool] = Query(default=True),
    db: Session = Depends(get_db),
):
    """List all configured category budget rules."""
    return [_to_read_model(item) for item in list_budget_rules_service(db, is_active=is_active)]


@router.post("", response_model=BudgetRuleRead, status_code=status.HTTP_200_OK)
def upsert_budget_rule(
    payload: BudgetRuleCreate,
    db: Session = Depends(get_db),
):
    """Create or update a spending target budget (weekly, monthly, or annual) for a category."""
    try:
        snapshot = upsert_budget_rule_service(
            db,
            BudgetRuleUpsertInput(
                category=payload.category,
                weekly_limit=payload.weekly_limit,
                monthly_limit=payload.monthly_limit,
                annual_limit=payload.annual_limit,
                budget_period=payload.budget_period,
                is_active=payload.is_active,
            ),
        )
    except BudgetRuleValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_read_model(snapshot)


@router.delete("/{budget_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_budget_rule(
    budget_id: int,
    db: Session = Depends(get_db),
):
    """Delete an existing category budget rule."""
    try:
        delete_budget_rule_service(db, budget_id)
    except BudgetRuleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return None


@router.post("/init-defaults", response_model=list[BudgetRuleRead])
def initialize_default_budgets(
    db: Session = Depends(get_db),
):
    """Initialize default sensible baseline category budgets if none are configured."""
    try:
        snapshots = initialize_default_budgets_service(db)
    except BudgetRuleValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [_to_read_model(item) for item in snapshots]


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


@router.post("/sync-from-burn", response_model=list[BudgetRuleRead])
def sync_budgets_from_burn(
    lookback_weeks: int = Query(default=12, ge=1, le=52),
    db: Session = Depends(get_db),
):
    """Auto-calculate and upsert weekly/monthly BudgetRules from actual average weekly burn."""
    try:
        snapshots = sync_budget_rules_from_burn_service(db, lookback_weeks=lookback_weeks)
    except BudgetRuleValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [_to_read_model(item) for item in snapshots]


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
