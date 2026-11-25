"""Account, institution, and asset snapshot schemas."""

from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


VALID_ACCOUNT_TYPES = {"checking", "savings", "brokerage", "kiwisaver", "crypto", "credit_card", "private_note"}
VALID_ACCOUNT_TIERS = {"cash", "growth", "locked"}


def normalize_account_type(value: Any) -> Optional[str]:
    """Normalize account_type to a valid canonical enum value."""
    if value is None:
        return None
    cleaned = str(value).strip().lower()
    if cleaned not in VALID_ACCOUNT_TYPES:
        raise ValueError(
            f"Unsupported account_type '{value}'. Must be one of: {', '.join(sorted(VALID_ACCOUNT_TYPES))}."
        )
    return cleaned


def normalize_account_tier(value: Any) -> Optional[str]:
    """Normalize account tier to cash, growth, or locked."""
    if value is None:
        return None
    cleaned = str(value).strip().lower()
    if cleaned not in VALID_ACCOUNT_TIERS:
        raise ValueError(
            f"Unsupported tier '{value}'. Must be one of: {', '.join(sorted(VALID_ACCOUNT_TIERS))}."
        )
    return cleaned


class AccountBase(BaseModel):
    name: str = Field(..., max_length=100)
    account_type: str = Field(..., description="checking, savings, brokerage, kiwisaver, crypto, credit_card, private_note")
    tier: str = Field(default="cash", description="cash, growth, locked")
    institution: str = Field(..., max_length=100)
    currency: str = Field(default="NZD", max_length=3)
    expected_annual_return: float = Field(default=0.0, ge=0.0, le=1.0)
    is_active: bool = True
    account_number_last4: Optional[str] = Field(default=None, max_length=64)

    @field_validator("account_type", mode="before")
    @classmethod
    def validate_account_type_base(cls, v: Any) -> str:
        resolved = normalize_account_type(v)
        if not resolved:
            raise ValueError("account_type is required.")
        return resolved

    @field_validator("tier", mode="before")
    @classmethod
    def validate_tier_base(cls, v: Any) -> str:
        resolved = normalize_account_tier(v)
        if not resolved:
            raise ValueError("tier is required.")
        return resolved


class AccountCreate(AccountBase):
    pass


class AccountUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=100)
    account_type: Optional[str] = Field(default=None, description="checking, savings, brokerage, kiwisaver, crypto, credit_card, private_note")
    tier: Optional[str] = Field(default=None, description="cash, growth, locked")
    institution: Optional[str] = Field(default=None, max_length=100)
    expected_annual_return: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    is_active: Optional[bool] = None
    account_number_last4: Optional[str] = Field(default=None, max_length=64)

    @field_validator("name", "institution")
    @classmethod
    def validate_non_empty_text(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Field cannot be empty.")
        return cleaned

    @field_validator("account_type", mode="before")
    @classmethod
    def validate_account_type_update(cls, v: Any) -> Optional[str]:
        return normalize_account_type(v)

    @field_validator("tier", mode="before")
    @classmethod
    def validate_tier_update(cls, v: Any) -> Optional[str]:
        return normalize_account_tier(v)


class AccountRead(AccountBase):
    id: int
    external_provider: Optional[str] = None
    external_account_id: Optional[str] = None
    last_synced_at: Optional[datetime] = None
    payout_total_native: float = 0.0
    payout_total_nzd: float = 0.0
    payout_count: int = 0
    first_payout_date: Optional[date] = None
    last_payout_date: Optional[date] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class InstitutionInfo(BaseModel):
    name: str = Field(..., max_length=100)
    default_tier: str = Field(default="cash", description="cash, growth, locked")
    default_type: str = Field(default="checking", description="checking, savings, brokerage, kiwisaver, crypto, credit_card, private_note")
    currency: str = Field(default="NZD", max_length=3)


class AssetSnapshotBase(BaseModel):
    snapshot_date: date
    account_id: int
    balance: float
    currency: str = "NZD"
    exchange_rate: Optional[float] = None
    source: str = "manual"
    notes: Optional[str] = None


class AssetSnapshotCreate(AssetSnapshotBase):
    exchange_rate: Optional[float] = Field(default=None, gt=0.0)


class AssetSnapshotRead(AssetSnapshotBase):
    id: int
    balance_nzd: float
    created_at: datetime

    class Config:
        from_attributes = True
