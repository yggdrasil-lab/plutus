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
from app.services.loan_service import get_loan_portfolio_summary
from app.services.runway import (
    BASELINE_ANNUAL_RETURN,
    calculate_compounding_runway,
    calculate_emergency_runway,
    get_balance_tier_snapshot,
    get_runway_snapshot,
)
from app.services.spending_projection import project_burn

router = APIRouter(prefix="/runway", tags=["Runway & Freedom Clock"])


@router.get("", response_model=RunwayResponse)
def get_current_runway(
    as_of: Optional[date] = Query(default=None),
    annual_return: Optional[float] = Query(
        default=BASELINE_ANNUAL_RETURN,
        ge=0.0,
        le=0.3,
        description="Real annual expected return",
    ),
    db: Session = Depends(get_db),
):
    """Calculate current financial runway in weeks based on asset balances, burn rate, and growth."""
    expected_return = annual_return if annual_return is not None else BASELINE_ANNUAL_RETURN
    snapshot = get_runway_snapshot(db, as_of=as_of, annual_return=expected_return)
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
def simulate_runway_scenario(
    payload: RunwayScenarioRequest,
    db: Session = Depends(get_db),
):
    """Simulate what-if changes to weekly burn rate, expected growth, or lump sum capital."""
    today = date.today()
    balances = get_balance_tier_snapshot(db)
    weekly_burn = project_burn(db, reference_date=today).effective_weekly_burn

    baseline_liquid = balances.total_liquid_balance
    locked_bal = balances.locked_balance
    loan_summary = get_loan_portfolio_summary(db, as_of=today)
    total_liabilities = loan_summary.total_liabilities
    loan_weekly_relief = loan_summary.total_weekly_payment

    baseline_weeks, baseline_perpetual = calculate_compounding_runway(
        accessible_assets=baseline_liquid,
        weekly_burn=weekly_burn,
        annual_real_return=BASELINE_ANNUAL_RETURN,
    )

    burn_delta = payload.weekly_burn_adjustment - (loan_weekly_relief if payload.pay_off_loans else 0.0)
    liquid_delta = payload.lump_sum_adjustment - (total_liabilities if payload.pay_off_loans else 0.0)
    simulated_burn = max(1.0, weekly_burn + burn_delta)
    simulated_liquid = max(0.0, baseline_liquid + liquid_delta)
    simulated_return = (
        payload.custom_annual_return
        if payload.custom_annual_return is not None
        else BASELINE_ANNUAL_RETURN
    )

    sim_weeks, is_perpetual = calculate_compounding_runway(
        accessible_assets=simulated_liquid,
        weekly_burn=simulated_burn,
        annual_real_return=simulated_return,
    )
    sim_years = round(sim_weeks / 52.0, 2) if sim_weeks is not None else None

    delta_weeks: Optional[float] = None
    if not is_perpetual and not baseline_perpetual and sim_weeks is not None and baseline_weeks is not None:
        delta_weeks = round(sim_weeks - baseline_weeks, 2)

    remaining_liabilities = 0.0 if payload.pay_off_loans else total_liabilities
    simulated_net_worth = round(simulated_liquid + locked_bal - remaining_liabilities, 2)

    return RunwayScenarioResponse(
        simulated_runway_weeks=sim_weeks,
        simulated_runway_years=sim_years,
        is_perpetual=is_perpetual,
        delta_weeks_compared_to_baseline=delta_weeks,
        loan_free_date=loan_summary.latest_payoff_date,
        loan_weekly_relief=loan_weekly_relief,
        total_liabilities=remaining_liabilities,
        simulated_net_worth=simulated_net_worth,
    )


@router.post("/synthesize", response_model=RunwayAiSynthesisRead)
def trigger_runway_synthesis(
    payload: RunwaySynthesizeRequest,
    db: Session = Depends(get_db),
    ai_gateway: AiGateway = Depends(get_ai_gateway),
):
    """Force on-demand research-grounded AI synthesis of financial freedom scenario and recommendations."""
    balances = get_balance_tier_snapshot(db)
    weekly_burn = project_burn(db, reference_date=date.today()).effective_weekly_burn
    cash_bal = balances.cash_balance
    growth_bal = balances.growth_balance
    locked_bal = balances.locked_balance
    total_liquid = balances.total_liquid_balance
    emergency_weeks = calculate_emergency_runway(cash_bal, weekly_burn)
    growth_weeks, is_perpetual = calculate_compounding_runway(
        accessible_assets=total_liquid,
        weekly_burn=weekly_burn,
        annual_real_return=BASELINE_ANNUAL_RETURN,
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
        gateway=ai_gateway,
    )
