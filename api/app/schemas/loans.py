"""Loan and repayment schedule schemas."""

from datetime import date
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


VALID_LOAN_TYPES = {"borrowed", "lent"}


def normalize_loan_type(value: Any) -> Optional[str]:
    """Normalize loan_type to 'borrowed' or 'lent'."""
    if value is None:
        return None
    cleaned = str(value).strip().lower()
    if cleaned not in VALID_LOAN_TYPES:
        raise ValueError(
            f"Unsupported loan_type '{value}'. Must be 'borrowed' or 'lent'."
        )
    return cleaned


class LoanBase(BaseModel):
    name: str = Field(..., max_length=120)
    lender: str = Field(..., max_length=120)
    loan_type: str = Field(default="borrowed", description="'borrowed' or 'lent'")
    principal_amount: float = Field(..., gt=0.0)
    annual_interest_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    term_months: int = Field(default=0, ge=0, le=600)
    monthly_payment: float = Field(default=0.0, ge=0.0)
    start_date: date
    asset_value: Optional[float] = Field(default=None, ge=0.0)
    currency: str = Field(default="NZD", max_length=3)
    notes: Optional[str] = None
    is_active: bool = True

    @field_validator("loan_type", mode="before")
    @classmethod
    def validate_loan_type_base(cls, v: Any) -> str:
        if v is None:
            return "borrowed"
        resolved = normalize_loan_type(v)
        assert resolved is not None
        return resolved

    @model_validator(mode="after")
    def validate_borrowed_terms(self) -> "LoanBase":
        if self.loan_type == "borrowed":
            if self.term_months < 1:
                raise ValueError("term_months must be >= 1 for borrowed loans.")
            if self.monthly_payment <= 0.0:
                raise ValueError("monthly_payment must be > 0 for borrowed loans.")
        return self


class LoanCreate(LoanBase):
    pass


class LoanUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    lender: Optional[str] = Field(default=None, max_length=120)
    loan_type: Optional[str] = Field(default=None, description="'borrowed' or 'lent'")
    principal_amount: Optional[float] = Field(default=None, gt=0.0)
    annual_interest_rate: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    term_months: Optional[int] = Field(default=None, ge=0, le=600)
    monthly_payment: Optional[float] = Field(default=None, ge=0.0)
    start_date: Optional[date] = None
    asset_value: Optional[float] = Field(default=None, ge=0.0)
    notes: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator("loan_type", mode="before")
    @classmethod
    def validate_loan_type_update(cls, v: Any) -> Optional[str]:
        return normalize_loan_type(v)

    @model_validator(mode="after")
    def validate_borrowed_terms_update(self) -> "LoanUpdate":
        if self.loan_type == "borrowed":
            if self.term_months is not None and self.term_months < 1:
                raise ValueError("term_months must be >= 1 for borrowed loans.")
            if self.monthly_payment is not None and self.monthly_payment <= 0.0:
                raise ValueError("monthly_payment must be > 0 for borrowed loans.")
        return self


class LoanPaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    split_id: Optional[int] = None
    transaction_date: date
    amount: float
    currency: str
    amount_nzd: float
    applied_amount: float
    loan_payment_amount: Optional[float] = None
    entry_type: str = "repayment"
    merchant: str
    account_id: int
    account_name: Optional[str] = None
    category: str
    notes: Optional[str] = None


class LoanRead(LoanBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    payments_made: int
    payments_remaining: int
    payoff_date: Optional[date] = None
    next_payment_date: Optional[date] = None
    remaining_principal: float
    principal_paid: float
    total_paid_amount: float = 0.0
    interest_paid: float = 0.0
    remaining_interest: float
    weekly_payment_equivalent: float
    net_worth_impact: float
    last_payment_date: Optional[date] = None
    is_payment_due: bool = False
    linked_payments: list[LoanPaymentRead] = Field(default_factory=list)
    disbursements: list[LoanPaymentRead] = Field(default_factory=list)


class LoansSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    loans: list[LoanRead] = Field(default_factory=list)
    total_liabilities: float
    total_receivables: float = 0.0
    total_asset_value: float
    net_loan_impact: float
    total_monthly_payment: float
    total_weekly_payment: float
    latest_payoff_date: Optional[date] = None
