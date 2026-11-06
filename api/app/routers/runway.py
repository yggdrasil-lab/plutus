"""Financial runway and time horizon router."""

from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Account, AssetSnapshot, BudgetRule, Transaction
from app.schemas import (
    RunwayAiSynthesisRead,
    RunwayResponse,
    RunwayScenarioRequest,
    RunwayScenarioResponse,
    RunwaySynthesizeRequest,
    RunwayTierBreakdown,
)
from app.services.llm_synthesizer import synthesize_runway_scenario_llm
from app.services.runway import (
    calculate_compounding_runway,
    calculate_emergency_runway,
    calculate_time_banked,
    calculate_weekly_growth_rate,
)

router = APIRouter(prefix="/runway", tags=["Runway & Freedom Clock"])


def get_latest_account_balances(db: Session) -> dict[str, float]:
    """Retrieve the latest balance in NZD for all active accounts grouped by tier."""
    accounts = db.scalars(select(Account).where(Account.is_active == True)).all()
    balances = {"cash": 0.0, "growth": 0.0, "locked": 0.0}

    for acc in accounts:
        latest_snapshot = db.scalar(
            select(AssetSnapshot)
            .where(AssetSnapshot.account_id == acc.id)
            .order_by(desc(AssetSnapshot.snapshot_date))
        )
        bal_nzd = float(latest_snapshot.balance_nzd) if latest_snapshot else 0.0
        tier = acc.tier.lower()
        if tier in balances:
            balances[tier] += bal_nzd
        else:
            balances["cash"] += bal_nzd

    return balances


def get_effective_weekly_burn(db: Session, as_of: date) -> tuple[float, float, float]:
    """Calculate effective weekly burn rate and current week spending vs budget.

    Returns:
        tuple of (effective_weekly_burn, this_week_spend, this_week_budget)
    """
    # 1. Budget sum
    budget_sum = db.scalar(select(func.sum(BudgetRule.weekly_limit)).where(BudgetRule.is_active == True))
    weekly_budget = float(budget_sum or 0.0)

    # 2. Trailing 4-week actual burn rate
    four_weeks_ago = as_of - timedelta(days=28)
    trailing_spend = db.scalar(
        select(func.sum(Transaction.amount)).where(
            Transaction.transaction_date >= four_weeks_ago,
            Transaction.transaction_date <= as_of,
            Transaction.amount < 0,  # Negative amounts represent expenses
        )
    )
    trailing_weekly_burn = abs(float(trailing_spend or 0.0)) / 4.0

    # 3. Spend in current calendar week (Monday to Sunday)
    start_of_week = as_of - timedelta(days=as_of.weekday())
    current_week_spend = db.scalar(
        select(func.sum(Transaction.amount)).where(
            Transaction.transaction_date >= start_of_week,
            Transaction.transaction_date <= as_of,
            Transaction.amount < 0,
        )
    )
    this_week_spend = abs(float(current_week_spend or 0.0))

    # Baseline burn: use trailing average if available, else budget, else default 1000 NZD/w
    if trailing_weekly_burn > 0.0:
        effective_burn = trailing_weekly_burn
    elif weekly_budget > 0.0:
        effective_burn = weekly_budget
    else:
        effective_burn = 1000.0

    target_budget = weekly_budget if weekly_budget > 0.0 else effective_burn
    return effective_burn, this_week_spend, target_budget


@router.get("", response_model=RunwayResponse)
def get_current_runway(
    as_of: Optional[date] = Query(default=None),
    annual_return: Optional[float] = Query(default=0.05, ge=0.0, le=0.3, description="Real annual expected return"),
    db: Session = Depends(get_db),
):
    """Calculate current financial runway in weeks based on asset balances, burn rate, and growth."""
    today = as_of or date.today()
    balances = get_latest_account_balances(db)
    weekly_burn, this_week_spend, target_budget = get_effective_weekly_burn(db, today)

    cash_bal = balances["cash"]
    growth_bal = balances["growth"]
    locked_bal = balances["locked"]
    total_liquid = cash_bal + growth_bal

    # Emergency runway (cash only, 0% growth)
    emergency_weeks = calculate_emergency_runway(cash_bal, weekly_burn)

    # Compounding growth runway (total liquid assets)
    growth_weeks, is_perpetual = calculate_compounding_runway(
        accessible_assets=total_liquid,
        weekly_burn=weekly_burn,
        annual_real_return=annual_return,
    )

    # Weekly yield at assumed growth rate
    weekly_g = calculate_weekly_growth_rate(annual_return)
    perpetual_threshold_income = round(total_liquid * weekly_g, 2)

    # Primary hero metric
    primary_weeks = 9999.0 if is_perpetual else (growth_weeks or emergency_weeks)
    primary_years = round(primary_weeks / 52.0, 2) if not is_perpetual else 999.0

    # Time banked this week
    time_banked = calculate_time_banked(target_budget, this_week_spend)

    # Locked runway: weeks of expenses preserved in locked/pension tier
    locked_weeks = round(locked_bal / weekly_burn, 1) if weekly_burn > 0 else 0.0

    # Resolve dynamic tier labels from actual active accounts
    active_accounts = db.scalars(select(Account).where(Account.is_active == True)).all()
    locked_accs = [a.name for a in active_accounts if a.tier.lower() == "locked"]
    locked_label = locked_accs[0] if len(locked_accs) == 1 else "Locked Reserve"
    growth_accs = [a.name for a in active_accounts if a.tier.lower() == "growth"]
    growth_label = growth_accs[0] if len(growth_accs) == 1 else "Growth"
    cash_accs = [a.name for a in active_accounts if a.tier.lower() == "cash" and a.account_type != "credit_card"]
    cash_label = cash_accs[0] if len(cash_accs) == 1 else "Cash Floor"

    tier_labels = {
        "cash": cash_label,
        "growth": growth_label,
        "locked": locked_label,
    }

    breakdown = RunwayTierBreakdown(
        cash_balance=cash_bal,
        growth_balance=growth_bal,
        locked_balance=locked_bal,
        total_liquid_balance=total_liquid,
        emergency_weeks=emergency_weeks,
        growth_weeks=growth_weeks,
        locked_weeks=locked_weeks,
        is_perpetual=is_perpetual,
        perpetual_threshold_weekly_income=perpetual_threshold_income,
        weekly_burn_rate=weekly_burn,
        tier_labels=tier_labels,
    )

    # Categories for quick spend tagging
    active_categories = db.scalars(
        select(BudgetRule.category)
        .where(BudgetRule.is_active == True)
        .order_by(BudgetRule.category)
    ).all()
    categories = list(active_categories)
    if not categories:
        tx_cats = db.scalars(select(Transaction.category).distinct()).all()
        categories = sorted(list(set(c for c in tx_cats if c)))
    if not categories:
        categories = ["Groceries", "Dining", "Transport", "Subscriptions", "Housing", "General"]

    return RunwayResponse(
        as_of_date=today,
        primary_runway_weeks=primary_weeks,
        primary_runway_years=primary_years,
        is_perpetual=is_perpetual,
        time_banked_this_week=time_banked,
        weekly_spend_this_week=this_week_spend,
        weekly_budget_target=target_budget,
        breakdown=breakdown,
        categories=categories,
    )


@router.post("/simulate", response_model=RunwayScenarioResponse)
def simulate_runway_scenario(
    payload: RunwayScenarioRequest,
    db: Session = Depends(get_db),
):
    """Simulate what-if changes to weekly burn rate, expected growth, or lump sum capital."""
    today = date.today()
    balances = get_latest_account_balances(db)
    weekly_burn, _, _ = get_effective_weekly_burn(db, today)

    # Baseline calculation
    baseline_liquid = balances["cash"] + balances["growth"]
    baseline_return = 0.05
    baseline_weeks, baseline_perpetual = calculate_compounding_runway(
        accessible_assets=baseline_liquid,
        weekly_burn=weekly_burn,
        annual_real_return=baseline_return,
    )

    # Simulated calculation
    simulated_burn = max(1.0, weekly_burn + payload.weekly_burn_adjustment)
    simulated_liquid = max(0.0, baseline_liquid + payload.lump_sum_adjustment)
    simulated_return = payload.custom_annual_return if payload.custom_annual_return is not None else baseline_return

    sim_weeks, is_perpetual = calculate_compounding_runway(
        accessible_assets=simulated_liquid,
        weekly_burn=simulated_burn,
        annual_real_return=simulated_return,
    )

    sim_years = round(sim_weeks / 52.0, 2) if sim_weeks is not None else None

    # Calculate delta
    delta_weeks: Optional[float] = None
    if not is_perpetual and not baseline_perpetual and sim_weeks is not None and baseline_weeks is not None:
        delta_weeks = round(sim_weeks - baseline_weeks, 2)

    return RunwayScenarioResponse(
        simulated_runway_weeks=sim_weeks,
        simulated_runway_years=sim_years,
        is_perpetual=is_perpetual,
        delta_weeks_compared_to_baseline=delta_weeks,
    )


@router.post("/synthesize", response_model=RunwayAiSynthesisRead)
def trigger_runway_synthesis(
    payload: RunwaySynthesizeRequest,
    db: Session = Depends(get_db),
):
    """Forces on-demand research-grounded AI synthesis of financial freedom scenario and recommendations."""
    today = date.today()
    balances = get_latest_account_balances(db)
    weekly_burn, _, _ = get_effective_weekly_burn(db, today)

    cash_bal = balances["cash"]
    growth_bal = balances["growth"]
    locked_bal = balances["locked"]
    total_liquid = cash_bal + growth_bal

    emergency_weeks = calculate_emergency_runway(cash_bal, weekly_burn)
    growth_weeks, is_perpetual = calculate_compounding_runway(
        accessible_assets=total_liquid,
        weekly_burn=weekly_burn,
        annual_real_return=0.05,
    )

    runway_telemetry = {
        "cash_balance": cash_bal,
        "growth_balance": growth_bal,
        "locked_balance": locked_bal,
        "total_liquid_balance": total_liquid,
        "weekly_burn_rate": weekly_burn,
        "emergency_weeks": emergency_weeks,
        "growth_weeks": growth_weeks,
        "is_perpetual": is_perpetual,
    }

    return synthesize_runway_scenario_llm(
        runway_data=runway_telemetry,
        burn_adjustment=payload.weekly_burn_adjustment,
        annual_return=payload.custom_annual_return,
        lump_sum=payload.lump_sum_adjustment,
        custom_context=payload.custom_context,
    )
