"""Transaction, split, and tag spending schemas."""

from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from ._common import normalize_recurrence, parse_tags_if_string


class TransactionBase(BaseModel):
    transaction_date: date
    account_id: int
    amount: float = Field(..., description="Negative for spending, positive for income")
    currency: str = "NZD"
    merchant: str = Field(..., max_length=150)
    category: str = Field(..., max_length=50)
    raw_description: Optional[str] = None
    notes: Optional[str] = None
    linked_transaction_id: Optional[int] = None
    exchange_rate: Optional[float] = None
    fee_amount: Optional[float] = None
    tags: list[str] = Field(default_factory=list)
    recurrence: Optional[str] = Field(default=None, max_length=20)

    @field_validator("recurrence", mode="before")
    @classmethod
    def validate_recurrence(cls, v: Any) -> Optional[str]:
        return normalize_recurrence(v)

    @field_validator("tags", mode="before")
    @classmethod
    def parse_tags_if_string(cls, v: Any) -> list[str]:
        return parse_tags_if_string(v)


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


class TransactionSplitInput(BaseModel):
    amount: float
    category: str
    tags: list[str] = Field(default_factory=list)
    loan_id: Optional[int] = None
    notes: Optional[str] = Field(default=None, max_length=255)

    @field_validator("amount")
    @classmethod
    def reject_zero_amount(cls, v: float) -> float:
        if float(v) == 0.0:
            raise ValueError("Split amount cannot be zero")
        return float(v)


class TransactionSplitsUpdate(BaseModel):
    splits: list[TransactionSplitInput] = Field(default_factory=list)


class TransactionSplitRead(BaseModel):
    id: int
    transaction_id: int
    amount: float
    category: str
    tags: list[str] = Field(default_factory=list)
    loan_id: Optional[int] = None
    loan_name: Optional[str] = None
    loan_type: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("tags", mode="before")
    @classmethod
    def parse_tags_if_string(cls, v: Any) -> list[str]:
        return parse_tags_if_string(v)

    class Config:
        from_attributes = True


class TransactionRead(TransactionBase):
    id: int
    account_name: Optional[str] = None
    linked_account_name: Optional[str] = None
    linked_amount: Optional[float] = None
    linked_currency: Optional[str] = None
    counterpart_account_id: Optional[int] = None
    counterpart_account_name: Optional[str] = None
    counterpart_account_type: Optional[str] = None
    loan_id: Optional[int] = None
    loan_name: Optional[str] = None
    loan_type: Optional[str] = None
    loan_payment_amount: Optional[float] = None
    raw_hash: str
    has_receipt: bool = False
    receipt_id: Optional[int] = None
    receipt_document_type: Optional[str] = None
    is_receipt_split: bool = False
    splits: list[TransactionSplitRead] = Field(default_factory=list)
    is_split: bool = False
    is_amortized_commitment: bool = False
    amortized_period_amount_nzd: Optional[float] = None
    amortized_cadence: Optional[str] = None
    excluded_from_period_budget: bool = False
    created_at: datetime

    class Config:
        from_attributes = True


class TransactionUpdate(BaseModel):
    category: Optional[str] = Field(default=None, max_length=50)
    merchant: Optional[str] = Field(default=None, max_length=150)
    notes: Optional[str] = None
    tags: Optional[list[str]] = None
    recurrence: Optional[str] = Field(default=None, max_length=20)
    counterpart_account_id: Optional[int] = None
    loan_id: Optional[int] = None
    loan_payment_amount: Optional[float] = Field(default=None, gt=0.0)

    @field_validator("recurrence", mode="before")
    @classmethod
    def validate_recurrence_update(cls, v: Any) -> Optional[str]:
        return normalize_recurrence(v)


class TagCategoryBreakdownItem(BaseModel):
    category: str
    total_spend: float
    transaction_count: int
    percentage: float


class TagSpendingSummaryItem(BaseModel):
    tag: str
    total_spend: float
    transaction_count: int
    first_date: date
    last_date: date
    categories: list[TagCategoryBreakdownItem] = Field(default_factory=list)


class TagSpendingSummaryResponse(BaseModel):
    period: str
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    total_tagged_spend: float
    tags: list[TagSpendingSummaryItem] = Field(default_factory=list)
