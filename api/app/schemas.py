"""Pydantic schemas for Plutus API."""

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field


# --- Account Schemas ---
class AccountBase(BaseModel):
    name: str = Field(..., max_length=100)
    account_type: str = Field(..., description="checking, savings, brokerage, kiwisaver, crypto, credit_card")
    tier: str = Field(default="cash", description="cash, growth, locked")
    institution: str = Field(..., max_length=100)
    currency: str = Field(default="NZD", max_length=3)
    expected_annual_return: float = Field(default=0.0, ge=0.0, le=1.0)
    is_active: bool = True


class AccountCreate(AccountBase):
    pass


class AccountRead(AccountBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# --- Institution Schemas ---
class InstitutionInfo(BaseModel):
    name: str = Field(..., max_length=100)
    default_tier: str = Field(default="cash", description="cash, growth, locked")
    default_type: str = Field(default="checking", description="checking, savings, brokerage, kiwisaver, crypto, credit_card")
    currency: str = Field(default="NZD", max_length=3)


# --- Asset Snapshot Schemas ---
class AssetSnapshotBase(BaseModel):
    snapshot_date: date
    account_id: int
    balance: float
    currency: str = "NZD"
    exchange_rate: float = 1.0
    source: str = "manual"
    notes: Optional[str] = None


class AssetSnapshotCreate(AssetSnapshotBase):
    pass


class AssetSnapshotRead(AssetSnapshotBase):
    id: int
    balance_nzd: float
    created_at: datetime

    class Config:
        from_attributes = True


# --- Transaction Schemas ---
class TransactionBase(BaseModel):
    transaction_date: date
    account_id: int
    amount: float = Field(..., description="Negative for spending, positive for income")
    currency: str = "NZD"
    merchant: str = Field(..., max_length=150)
    category: str = Field(..., max_length=50)
    raw_description: Optional[str] = None
    notes: Optional[str] = None


class TransactionCreate(TransactionBase):
    pass


class QuickLogRequest(BaseModel):
    """Frictionless quick expense entry from iOS Shortcuts, Siri, or PWA keypad."""
    amount: float = Field(..., gt=0.0, description="Absolute spend amount")
    merchant: str = Field(..., max_length=150)
    category: str = Field(default="General", max_length=50)
    account_name: Optional[str] = Field(default=None, description="Target account name, defaults to primary checking")
    transaction_date: Optional[date] = Field(default=None, description="Defaults to today")
    notes: Optional[str] = None


class TransactionRead(TransactionBase):
    id: int
    account_name: Optional[str] = None
    raw_hash: str
    created_at: datetime

    class Config:
        from_attributes = True


class TransactionUpdate(BaseModel):
    category: Optional[str] = Field(default=None, max_length=50)
    merchant: Optional[str] = Field(default=None, max_length=150)
    notes: Optional[str] = None


# --- Budget Rule Schemas ---
class BudgetRuleBase(BaseModel):
    category: str = Field(..., max_length=50)
    weekly_limit: float = Field(..., ge=0.0)
    monthly_limit: float = Field(..., ge=0.0)
    is_active: bool = True


class BudgetRuleCreate(BaseModel):
    category: str = Field(..., max_length=50)
    weekly_limit: Optional[float] = Field(default=None, ge=0.0)
    monthly_limit: Optional[float] = Field(default=None, ge=0.0)
    is_active: bool = True


class BudgetRuleRead(BudgetRuleBase):
    id: int

    class Config:
        from_attributes = True


class CategorySpendingReport(BaseModel):
    category: str
    total_spend: float
    transaction_count: int
    percentage: float
    budget_limit: Optional[float] = None
    percent_used: Optional[float] = None
    is_over_budget: bool = False
    warning_level: str = "normal"
    is_transfer: bool = False


class SpendingBreakdownResponse(BaseModel):
    period: str
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    total_spend: float
    total_income: float
    total_transfers: float = 0.0
    net_savings: float
    categories: list[CategorySpendingReport] = Field(default_factory=list)
    over_budget_count: int = 0


# --- Runway & Time-Horizon Schemas ---
class RunwayTierBreakdown(BaseModel):
    cash_balance: float
    growth_balance: float
    locked_balance: float
    total_liquid_balance: float
    emergency_weeks: float
    growth_weeks: Optional[float] = None
    locked_weeks: Optional[float] = None
    is_perpetual: bool
    perpetual_threshold_weekly_income: float
    weekly_burn_rate: float
    tier_labels: dict[str, str] = Field(default_factory=dict)


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


class RunwayScenarioResponse(BaseModel):
    simulated_runway_weeks: Optional[float]
    simulated_runway_years: Optional[float]
    is_perpetual: bool
    delta_weeks_compared_to_baseline: Optional[float]


class RunwaySynthesizeRequest(BaseModel):
    weekly_burn_adjustment: float = Field(default=0.0, description="Proposed adjustment in $/week")
    custom_annual_return: Optional[float] = Field(default=None, ge=0.0, le=0.5, description="Custom real return rate")
    lump_sum_adjustment: float = Field(default=0.0, description="One-time asset injection or withdrawal")
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


from app.services.statement_parser import ParsedTransaction, StatementParseResult
