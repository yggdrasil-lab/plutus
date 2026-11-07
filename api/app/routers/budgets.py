"""Budgets and category spending guardrails router."""

from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, desc, func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import BudgetRule, Transaction
from app.schemas import (
    BudgetRuleCreate,
    BudgetRuleRead,
    CategorySpendingReport,
    SpendingBreakdownResponse,
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
    """Create or update a spending target budget for a given category."""
    cat = payload.category.strip()
    if not cat:
        raise HTTPException(status_code=400, detail="Category name cannot be empty")

    weekly = payload.weekly_limit
    monthly = payload.monthly_limit

    if weekly is None and monthly is None:
        weekly = 100.0
        monthly = round(weekly * 52 / 12, 2)
    elif weekly is not None and monthly is None:
        monthly = round(weekly * 52 / 12, 2)
    elif monthly is not None and weekly is None:
        weekly = round(monthly * 12 / 52, 2)

    assert weekly is not None and monthly is not None

    existing = db.scalar(select(BudgetRule).where(func.lower(BudgetRule.category) == cat.lower()))
    if existing:
        existing.weekly_limit = weekly
        existing.monthly_limit = monthly
        existing.is_active = payload.is_active
        db.commit()
        db.refresh(existing)
        return existing

    new_rule = BudgetRule(
        category=cat,
        weekly_limit=weekly,
        monthly_limit=monthly,
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
        for r in created:
            db.refresh(r)
    return db.scalars(select(BudgetRule).order_by(BudgetRule.category)).all()


@router.get("/breakdown", response_model=SpendingBreakdownResponse)
def get_spending_breakdown(
    period: str = Query(default="month", description="'week', 'month', or 'all'"),
    db: Session = Depends(get_db),
):
    """Retrieve detailed category spending breakdown, budget utilization, and overspending warnings."""
    today = date.today()
    start_date: Optional[date] = None
    end_date: Optional[date] = None

    if period == "week":
        # Monday of current week
        start_date = today - timedelta(days=today.weekday())
        end_date = start_date + timedelta(days=6)
    elif period == "month":
        # 1st day of current month
        start_date = date(today.year, today.month, 1)
        end_date = today
    elif period == "all":
        start_date = None
        end_date = None
    else:
        raise HTTPException(status_code=400, detail="Invalid period. Choose 'week', 'month', or 'all'.")

    # Fetch active budget rules
    budget_rules = {r.category.lower(): r for r in db.scalars(select(BudgetRule).where(BudgetRule.is_active == True)).all()}

    # Base transaction query
    stmt = select(Transaction)
    if start_date:
        stmt = stmt.where(Transaction.transaction_date >= start_date)
    if end_date:
        stmt = stmt.where(Transaction.transaction_date <= end_date)

    txs = db.scalars(stmt).all()

    total_spend = 0.0
    total_income = 0.0
    category_spend_map: dict[str, dict] = {}

    for tx in txs:
        amt = float(tx.amount)
        if amt < 0:
            # Expense
            abs_spend = abs(amt)
            total_spend += abs_spend
            cat = tx.category or "General"
            if cat not in category_spend_map:
                category_spend_map[cat] = {"spend": 0.0, "count": 0}
            category_spend_map[cat]["spend"] += abs_spend
            category_spend_map[cat]["count"] += 1
        elif amt > 0:
            # Income
            total_income += amt

    total_spend = round(total_spend, 2)
    total_income = round(total_income, 2)
    net_savings = round(total_income - total_spend, 2)

    # Compile category reports
    reports: list[CategorySpendingReport] = []
    processed_lower_cats = set()

    for cat, data_dict in category_spend_map.items():
        cat_spend = round(data_dict["spend"], 2)
        count = data_dict["count"]
        pct = round((cat_spend / total_spend * 100), 1) if total_spend > 0 else 0.0
        processed_lower_cats.add(cat.lower())

        rule = budget_rules.get(cat.lower())
        budget_limit: Optional[float] = None
        percent_used: Optional[float] = None
        is_over = False
        warning = "normal"

        if rule:
            budget_limit = float(rule.weekly_limit if period == "week" else rule.monthly_limit)
            if budget_limit > 0:
                percent_used = round((cat_spend / budget_limit * 100), 1)
                is_over = cat_spend > budget_limit
                if is_over:
                    warning = "danger"
                elif percent_used >= 80.0:
                    warning = "warning"

        reports.append(
            CategorySpendingReport(
                category=cat,
                total_spend=cat_spend,
                transaction_count=count,
                percentage=pct,
                budget_limit=budget_limit,
                percent_used=percent_used,
                is_over_budget=is_over,
                warning_level=warning,
            )
        )

    # Include budgeted categories that have 0 spending in this period
    for cat_lower, rule in budget_rules.items():
        if cat_lower not in processed_lower_cats:
            budget_limit = float(rule.weekly_limit if period == "week" else rule.monthly_limit)
            reports.append(
                CategorySpendingReport(
                    category=rule.category,
                    total_spend=0.0,
                    transaction_count=0,
                    percentage=0.0,
                    budget_limit=budget_limit,
                    percent_used=0.0,
                    is_over_budget=False,
                    warning_level="normal",
                )
            )

    # Sort: highest spend first, then alphabetical
    reports.sort(key=lambda r: (-r.total_spend, r.category))

    over_budget_count = sum(1 for r in reports if r.is_over_budget)

    return SpendingBreakdownResponse(
        period=period,
        start_date=start_date,
        end_date=end_date,
        total_spend=total_spend,
        total_income=total_income,
        net_savings=net_savings,
        categories=reports,
        over_budget_count=over_budget_count,
    )
