"""Hierarchical category schemas."""

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


VALID_AMORTIZE_PERIODS = {"annual"}


def normalize_amortize_period(value: Any) -> Optional[str]:
    """Normalize category amortization rule ('annual' for 52w spread, or None for direct)."""
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized or normalized in ("none", "direct", "variable"):
        return None
    if normalized not in VALID_AMORTIZE_PERIODS:
        raise ValueError(
            f"Unsupported amortize_period '{value}'. Must be 'annual' or null."
        )
    return normalized


class CategoryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    parent_id: Optional[int] = None
    amortize_period: Optional[str] = Field(default=None, max_length=20)
    sort_order: Optional[int] = Field(default=None, ge=0, le=10000)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Category name cannot be empty.")
        return cleaned

    @field_validator("amortize_period", mode="before")
    @classmethod
    def validate_amortize_period(cls, v: Any) -> Optional[str]:
        return normalize_amortize_period(v)


class CategoryUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=50)
    parent_id: Optional[int] = None
    amortize_period: Optional[str] = Field(default=None, max_length=20)
    is_active: Optional[bool] = None
    sort_order: Optional[int] = Field(default=None, ge=0, le=10000)

    @field_validator("name")
    @classmethod
    def validate_name_update(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Category name cannot be empty.")
        return cleaned

    @field_validator("amortize_period", mode="before")
    @classmethod
    def validate_amortize_period_update(cls, v: Any) -> Optional[str]:
        return normalize_amortize_period(v)


class CategoryRead(BaseModel):
    id: int
    name: str
    parent_id: Optional[int] = None
    parent_name: Optional[str] = None
    is_system: bool = False
    is_active: bool = True
    amortize_period: Optional[str] = None
    sort_order: int = 100
    transaction_count: int = 0
    rule_count: int = 0
    child_count: int = 0
    has_budget_rule: bool = False
    budget_period: Optional[str] = None
    weekly_limit: Optional[float] = None
    monthly_limit: Optional[float] = None
    annual_limit: Optional[float] = None

    class Config:
        from_attributes = True


class CategoryDeleteResult(BaseModel):
    deleted_id: int
    deleted_name: str
    reassigned_to: str
    transactions_reassigned: int
    splits_reassigned: int = 0
    rules_reassigned: int
    receipt_items_reassigned: int
    products_reassigned: int
    children_promoted: int
