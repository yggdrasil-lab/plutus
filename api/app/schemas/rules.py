"""Categorization rule and batch classification schemas."""

from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from ._common import normalize_recurrence, parse_tags_if_string


VALID_RULE_DIRECTIONS = {"outflow", "inflow"}


def normalize_rule_direction(value: Any) -> Optional[str]:
    """Normalize optional rule cashflow direction to 'outflow', 'inflow', or None."""
    if value is None:
        return None
    cleaned = str(value).strip().lower()
    if not cleaned:
        return None
    if cleaned not in VALID_RULE_DIRECTIONS:
        raise ValueError(
            f"Unsupported direction '{value}'. Must be 'outflow', 'inflow', or null."
        )
    return cleaned


class CategorizationRuleBase(BaseModel):
    name: str = Field(..., max_length=100)
    description_pattern: str = Field(..., max_length=200)
    match_type: str = Field(default="contains", description="'contains', 'exact', or 'starts_with'")
    amount_equals: Optional[float] = Field(default=None, ge=0.0)
    min_amount: Optional[float] = Field(default=None, ge=0.0)
    max_amount: Optional[float] = Field(default=None, ge=0.0)
    direction: Optional[str] = Field(default=None, max_length=20)
    target_category: str = Field(..., max_length=50)
    target_tags: list[str] = Field(default_factory=list)
    target_recurrence: Optional[str] = Field(default=None, max_length=20)
    rename_merchant: Optional[str] = Field(default=None, max_length=150)
    counterpart_account_id: Optional[int] = None
    loan_id: Optional[int] = None
    priority: int = Field(default=10, ge=1)
    is_active: bool = True

    @field_validator("target_tags", mode="before")
    @classmethod
    def parse_target_tags_if_string(cls, v: Any) -> list[str]:
        return parse_tags_if_string(v)

    @field_validator("target_recurrence", mode="before")
    @classmethod
    def validate_target_recurrence(cls, v: Any) -> Optional[str]:
        return normalize_recurrence(v)

    @field_validator("direction", mode="before")
    @classmethod
    def validate_direction(cls, v: Any) -> Optional[str]:
        return normalize_rule_direction(v)

    @model_validator(mode="after")
    def validate_amount_bounds(self) -> "CategorizationRuleBase":
        if (
            self.min_amount is not None
            and self.max_amount is not None
            and float(self.min_amount) > float(self.max_amount)
        ):
            raise ValueError("min_amount must be less than or equal to max_amount.")
        return self


class CategorizationRuleCreate(CategorizationRuleBase):
    apply_retroactively: bool = Field(default=True, description="Apply immediately to matching historical transactions")


class CategorizationRuleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=100)
    description_pattern: Optional[str] = Field(default=None, max_length=200)
    match_type: Optional[str] = None
    amount_equals: Optional[float] = None
    min_amount: Optional[float] = Field(default=None, ge=0.0)
    max_amount: Optional[float] = Field(default=None, ge=0.0)
    direction: Optional[str] = Field(default=None, max_length=20)
    target_category: Optional[str] = Field(default=None, max_length=50)
    target_tags: Optional[list[str]] = None
    target_recurrence: Optional[str] = Field(default=None, max_length=20)
    rename_merchant: Optional[str] = Field(default=None, max_length=150)
    counterpart_account_id: Optional[int] = None
    loan_id: Optional[int] = None
    priority: Optional[int] = None
    is_active: Optional[bool] = None
    apply_retroactively: bool = False

    @field_validator("target_recurrence", mode="before")
    @classmethod
    def validate_target_recurrence_update(cls, v: Any) -> Optional[str]:
        return normalize_recurrence(v)

    @field_validator("direction", mode="before")
    @classmethod
    def validate_direction_update(cls, v: Any) -> Optional[str]:
        return normalize_rule_direction(v)

    @model_validator(mode="after")
    def validate_amount_bounds_update(self) -> "CategorizationRuleUpdate":
        if (
            self.min_amount is not None
            and self.max_amount is not None
            and float(self.min_amount) > float(self.max_amount)
        ):
            raise ValueError("min_amount must be less than or equal to max_amount.")
        return self


class CategorizationRuleRead(CategorizationRuleBase):
    id: int
    counterpart_account_name: Optional[str] = None
    loan_name: Optional[str] = None
    loan_type: Optional[str] = None
    match_count: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ClassifyUncategorizedRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=500)
    include_untagged: bool = False
    dry_run: bool = True
    only_outflows: bool = False


class ClassifyUncategorizedItem(BaseModel):
    transaction_id: int
    transaction_date: date
    merchant: str
    amount: float
    currency: str
    old_category: str
    new_category: str
    old_tags: list[str]
    new_tags: list[str]


class ClassifyUncategorizedResponse(BaseModel):
    dry_run: bool
    evaluated: int
    updated: int
    skipped_circuit_open: int
    remaining: int
    changes: list[ClassifyUncategorizedItem]
