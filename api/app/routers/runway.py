"""Financial runway and time horizon router."""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import (
    RunwayAiSynthesisRead,
    RunwayResponse,
    RunwayScenarioRequest,
    RunwayScenarioResponse,
    RunwaySynthesizeRequest,
    RunwayTierBreakdown,
)
from app.services.ai_gateway import AiGateway, get_ai_gateway
from app.services.category_taxonomy import get_active_categories
from app.services.llm_synthesizer import synthesize_runway_scenario_llm
from app.services.runway import get_runway_snapshot, simulate_runway_scenario

router = APIRouter(prefix="/runway", tags=["Runway & Freedom Clock"])


@router.get("", response_model=RunwayResponse)
def get_current_runway(
    as_of: Optional[date] = Query(default=None),
    annual_return: Optional[float] = Query(
        default=None,
        ge=0.0,
        le=0.3,
        description="Real annual expected return",
    ),
    db: Session = Depends(get_db),
):
    """Calculate current financial runway in weeks based on asset balances, burn rate, and growth."""
    snapshot = get_runway_snapshot(db, as_of=as_of, annual_return=annual_return)
    breakdown = RunwayTierBreakdown(
        cash_balance=snapshot.cash_balance,
        growth_balance=snapshot.growth_balance,
        locked_balance=snapshot.locked_balance,
        total_liquid_balance=snapshot.total_liquid_balance,
        total_liabilities=snapshot.total_liabilities,
        net_worth=snapshot.net_worth,
        emergency_weeks=snapshot.emergency_weeks,
        growth_weeks=snapshot.growth_weeks,
        locked_weeks=snapshot.locked_weeks,
        is_perpetual=snapshot.is_perpetual,
        perpetual_threshold_weekly_income=snapshot.perpetual_threshold_weekly_income,
        weekly_burn_rate=snapshot.effective_weekly_burn,
        variable_weekly_burn=snapshot.variable_weekly_burn,
        recurring_weekly_burn=snapshot.recurring_weekly_burn,
        this_week_variable_spend=snapshot.this_week_variable_spend,
        tier_labels=snapshot.tier_labels.as_dict(),
    )
    return RunwayResponse(
        as_of_date=snapshot.as_of_date,
        primary_runway_weeks=snapshot.primary_runway_weeks,
        primary_runway_years=snapshot.primary_runway_years,
        is_perpetual=snapshot.is_perpetual,
        time_banked_this_week=snapshot.time_banked_this_week,
        weekly_spend_this_week=snapshot.this_week_spend,
        weekly_budget_target=snapshot.weekly_budget_target,
        breakdown=breakdown,
        categories=get_active_categories(db),
    )


@router.post("/simulate", response_model=RunwayScenarioResponse)
def simulate_runway_scenario_endpoint(
    payload: RunwayScenarioRequest,
    db: Session = Depends(get_db),
):
    """Simulate what-if changes to weekly burn rate, expected growth, or lump sum capital."""
    simulation = simulate_runway_scenario(
        db,
        weekly_burn_adjustment=payload.weekly_burn_adjustment,
        custom_annual_return=payload.custom_annual_return,
        lump_sum_adjustment=payload.lump_sum_adjustment,
        pay_off_loans=payload.pay_off_loans,
    )
    return RunwayScenarioResponse(
        simulated_runway_weeks=simulation.simulated_runway_weeks,
        simulated_runway_years=simulation.simulated_runway_years,
        is_perpetual=simulation.is_perpetual,
        delta_weeks_compared_to_baseline=simulation.delta_weeks_compared_to_baseline,
        loan_free_date=simulation.loan_free_date,
        loan_weekly_relief=simulation.loan_weekly_relief,
        total_liabilities=simulation.remaining_liabilities,
        simulated_net_worth=simulation.simulated_net_worth,
    )


@router.post("/synthesize", response_model=RunwayAiSynthesisRead)
def trigger_runway_synthesis(
    payload: RunwaySynthesizeRequest,
    db: Session = Depends(get_db),
    ai_gateway: AiGateway = Depends(get_ai_gateway),
):
    """Force on-demand research-grounded AI synthesis of financial freedom scenario and recommendations."""
    simulation = simulate_runway_scenario(
        db,
        weekly_burn_adjustment=payload.weekly_burn_adjustment,
        custom_annual_return=payload.custom_annual_return,
        lump_sum_adjustment=payload.lump_sum_adjustment,
        pay_off_loans=payload.pay_off_loans,
    )
    return synthesize_runway_scenario_llm(
        simulation,
        custom_context=payload.custom_context,
        gateway=ai_gateway,
    )
