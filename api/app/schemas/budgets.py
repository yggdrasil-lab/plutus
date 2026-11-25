"""Budget rule, spending breakdown, and burn report schemas."""

from datetime import date
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


VALID_BUDGET_PERIODS = {"weekly", "monthly", "annual"}


def normalize_budget_period(value: Any) -> Optional[str]:
    """Normalize budget_period to weekly, monthly, or annual."""
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized not in VALID_BUDGET_PERIODS:
        raise ValueError(f"Unsupported budget_period '{value}'. Must be 'weekly', 'monthly', or 'annual'.")
    return normalized


class BudgetRuleBase(BaseModel):
    category: str = Field(..., max_length=50)
    weekly_limit: float = Field(..., ge=0.0)
    monthly_limit: float = Field(..., ge=0.0)
    budget_period: str = Field(default="monthly", max_length=20)
    is_active: bool = True

    @field_validator("budget_period", mode="before")
    @classmethod
    def validate_budget_period_base(cls, v: Any) -> str:
        return normalize_budget_period(v) or "monthly"


class BudgetRuleCreate(BaseModel):
    category: str = Field(..., max_length=50)
    weekly_limit: Optional[float] = Field(default=None, ge=0.0)
    monthly_limit: Optional[float] = Field(default=None, ge=0.0)
    annual_limit: Optional[float] = Field(default=None, ge=0.0)
    budget_period: Optional[str] = Field(default=None, max_length=20)
    is_active: bool = True

    @field_validator("budget_period", mode="before")
    @classmethod
    def validate_budget_period_create(cls, v: Any) -> Optional[str]:
        return normalize_budget_period(v)


class BudgetRuleRead(BudgetRuleBase):
    id: int
    annual_limit: float = 0.0

    @model_validator(mode="after")
    def populate_annual_limit(self) -> "BudgetRuleRead":
        self.annual_limit = round(float(self.weekly_limit) * 52.0, 2)
        return self

    class Config:
        from_attributes = True


class CategorySpendingReport(BaseModel):
    category: str
    parent_category: Optional[str] = None
    total_spend: float
    transaction_count: int
    actual_transaction_count: int = 0
    amortized_count: int = 0
    percentage: float
    budget_limit: Optional[float] = None
    percent_used: Optional[float] = None
    is_over_budget: bool = False
    warning_level: str = "normal"
    is_transfer: bool = False
    budget_period: str = "monthly"
    annual_limit: Optional[float] = None


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


class CategoryBurnEstimateRead(BaseModel):
    category: str
    parent_category: Optional[str] = None
    variable_weekly_burn: float
    recurring_weekly_burn: float
    average_weekly_burn: float
    suggested_monthly_limit: float
    suggested_annual_limit: float = 0.0
    budget_period: str = "monthly"
    variable_percentage: float = 0.0
    total_percentage: float = 0.0


class RecurringCommitmentRead(BaseModel):
    transaction_id: int
    merchant: str
    category: str
    recurrence: str
    last_charged_date: date
    original_amount: float
    currency: str
    amount_nzd: float
    weekly_nzd: float
    monthly_nzd: float
    percentage_of_recurring: float


class AverageBurnReportResponse(BaseModel):
    lookback_weeks: int
    as_of_date: date
    variable_weekly_burn: float
    recurring_weekly_burn: float
    effective_weekly_burn: float
    categories: list[CategoryBurnEstimateRead] = Field(default_factory=list)
    recurring_commitments: list[RecurringCommitmentRead] = Field(default_factory=list)
