"""Runway and time-horizon calculation schemas."""

from datetime import date
from typing import Optional

from pydantic import BaseModel, Field


class CreditBackstopSummary(BaseModel):
    total_credit_limit_nzd: float = 0.0
    total_credit_owed_nzd: float = 0.0
    total_available_credit_nzd: float = 0.0
    credit_utilization_pct: float = 0.0
    backstop_weeks: float = 0.0
    credit_card_available_nzd: float = 0.0
    credit_line_available_nzd: float = 0.0
    account_count: int = 0


class RunwayTierBreakdown(BaseModel):
    cash_balance: float
    growth_balance: float
    locked_balance: float
    total_liquid_balance: float
    total_liabilities: float = 0.0
    net_worth: float = 0.0
    emergency_weeks: float
    growth_weeks: Optional[float] = None
    locked_weeks: Optional[float] = None
    is_perpetual: bool
    perpetual_threshold_weekly_income: float
    weekly_burn_rate: float
    variable_weekly_burn: float = 0.0
    recurring_weekly_burn: float = 0.0
    this_week_variable_spend: float = 0.0
    tier_labels: dict[str, str] = Field(default_factory=dict)
    credit_backstop: CreditBackstopSummary = Field(default_factory=CreditBackstopSummary)


class RunwayResponse(BaseModel):
    as_of_date: date
    primary_runway_weeks: float
    primary_runway_years: float
    is_perpetual: bool
    time_banked_this_week: float
    weekly_spend_this_week: float
    weekly_budget_target: float
    breakdown: RunwayTierBreakdown
    categories: list[str] = Field(default_factory=list)


class RunwayScenarioRequest(BaseModel):
    weekly_burn_adjustment: float = Field(default=0.0, description="Adjustment in $/week (e.g. -150 to reduce burn)")
    custom_annual_return: Optional[float] = Field(default=None, ge=0.0, le=0.5, description="Custom real return rate")
    lump_sum_adjustment: float = Field(default=0.0, description="One-time asset injection or withdrawal")
    pay_off_loans: bool = Field(default=False, description="Simulate paying off active loans from liquid assets")


class RunwayScenarioResponse(BaseModel):
    simulated_runway_weeks: Optional[float]
    simulated_runway_years: Optional[float]
    is_perpetual: bool
    delta_weeks_compared_to_baseline: Optional[float]
    loan_free_date: Optional[date] = None
    loan_weekly_relief: float = 0.0
    total_liabilities: float = 0.0
    simulated_net_worth: float = 0.0


class RunwaySynthesizeRequest(BaseModel):
    weekly_burn_adjustment: float = Field(default=0.0, description="Proposed adjustment in $/week")
    custom_annual_return: Optional[float] = Field(default=None, ge=0.0, le=0.5, description="Custom real return rate")
    lump_sum_adjustment: float = Field(default=0.0, description="One-time asset injection or withdrawal")
    pay_off_loans: bool = Field(default=False, description="Simulate paying off active loans from liquid assets")
    custom_context: Optional[str] = Field(default=None, description="User notes, lifestyle shifts, or sabbatical goals")


class RunwayAiSynthesisRead(BaseModel):
    headline: str
    recommendations: list[str] = Field(default_factory=list)
    rationale: str
    research_citation: str
    impact_summary: str
    is_llm_generated: bool = True
    simulated_runway_weeks: Optional[float] = None
    simulated_runway_years: Optional[float] = None
    is_perpetual: bool = False
    delta_weeks_compared_to_baseline: Optional[float] = None
