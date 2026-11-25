"""Pydantic schemas for Plutus API."""

from datetime import date, datetime
import json
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


VALID_BUDGET_PERIODS = {"weekly", "monthly", "annual"}
VALID_AMORTIZE_PERIODS = {"annual"}


def normalize_budget_period(value: Any) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized not in VALID_BUDGET_PERIODS:
        raise ValueError(f"Unsupported budget_period '{value}'. Must be 'weekly', 'monthly', or 'annual'.")
    return normalized


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


# --- Account Schemas ---
VALID_ACCOUNT_TYPES = {"checking", "savings", "brokerage", "kiwisaver", "crypto", "credit_card", "private_note"}
VALID_ACCOUNT_TIERS = {"cash", "growth", "locked"}


def normalize_account_type(value: Any) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip().lower()
    if cleaned not in VALID_ACCOUNT_TYPES:
        raise ValueError(
            f"Unsupported account_type '{value}'. Must be one of: {', '.join(sorted(VALID_ACCOUNT_TYPES))}."
        )
    return cleaned


def normalize_account_tier(value: Any) -> Optional[str]:
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


# --- Institution Schemas ---
class InstitutionInfo(BaseModel):
    name: str = Field(..., max_length=100)
    default_tier: str = Field(default="cash", description="cash, growth, locked")
    default_type: str = Field(default="checking", description="checking, savings, brokerage, kiwisaver, crypto, credit_card, private_note")
    currency: str = Field(default="NZD", max_length=3)


# --- Asset Snapshot Schemas ---
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


RECURRENCE_CHOICES = {"weekly", "fortnightly", "monthly", "quarterly", "annual", "one-off"}


def normalize_recurrence(value: Any) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip().lower().replace("_", "-")
    if not cleaned or cleaned in ("none", "variable"):
        return None
    if cleaned not in RECURRENCE_CHOICES:
        raise ValueError(
            f"Unsupported recurrence '{value}'. Use one of: {', '.join(sorted(RECURRENCE_CHOICES))}."
        )
    return cleaned


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
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [str(t) for t in parsed if str(t).strip()]
            except Exception:
                return [t.strip() for t in v.split(",") if t.strip()]
        if isinstance(v, (list, tuple)):
            return [str(t) for t in v if str(t).strip()]
        return []


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
        return TransactionBase.parse_tags_if_string(v)

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


# --- Budget Rule Schemas ---
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


# --- Hierarchical Category Schemas ---
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


# --- Outlook-style Categorization Rule Schemas ---
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
        return TransactionBase.parse_tags_if_string(v)

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


# --- Product Catalog & Price History Schemas ---
class PricePoint(BaseModel):
    """Historical unit price record from an itemized receipt."""
    date: date
    merchant: str
    unit_price: float
    discount_amount: Optional[float] = None
    total_price: float
    quantity: float
    receipt_id: int
    receipt_item_id: int


class ProductBase(BaseModel):
    """Core inventory/catalog product attributes."""
    name: str = Field(..., max_length=200)
    brand: Optional[str] = Field(default=None, max_length=100)
    category: str = Field(default="General", max_length=50)
    domain: Optional[str] = Field(default=None, max_length=50)
    tags: list[str] = Field(default_factory=list)
    volume_ml: Optional[str] = Field(default=None, max_length=50)
    notes: Optional[str] = None
    rating: Optional[int] = Field(default=None, ge=1, le=5)
    is_favorite: bool = False

    @field_validator("tags", mode="before")
    @classmethod
    def parse_tags_if_string(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [str(t) for t in parsed]
            except Exception:
                return [t.strip() for t in v.split(",") if t.strip()]
        if isinstance(v, list):
            return [str(t) for t in v]
        return []


class ProductCreate(ProductBase):
    pass


class ProductUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    brand: Optional[str] = Field(default=None, max_length=100)
    category: Optional[str] = Field(default=None, max_length=50)
    domain: Optional[str] = Field(default=None, max_length=50)
    tags: Optional[list[str]] = None
    volume_ml: Optional[str] = Field(default=None, max_length=50)
    notes: Optional[str] = None
    rating: Optional[int] = Field(default=None, ge=1, le=5)
    is_favorite: Optional[bool] = None


class ProductRead(ProductBase):
    id: int
    purchase_count: int = 0
    total_spent: float = 0.0
    latest_price: Optional[float] = None
    lowest_price: Optional[float] = None
    highest_price: Optional[float] = None
    average_price: Optional[float] = None
    price_change_latest: Optional[float] = None  # Price difference compared to prior purchase
    first_purchased: Optional[date] = None
    last_purchased: Optional[date] = None
    avg_days_between_purchases: Optional[float] = None
    estimated_depletion_date: Optional[date] = None
    price_history: list[PricePoint] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ProductListResponse(BaseModel):
    total_count: int
    all_tags: list[str] = Field(default_factory=list)
    products: list[ProductRead] = Field(default_factory=list)


# --- Receipt & Itemized Product Telemetry Schemas ---
class ReceiptItemBase(BaseModel):
    item_name: str = Field(..., max_length=200)
    product_id: Optional[int] = None
    brand: Optional[str] = Field(default=None, max_length=100)
    quantity: float = Field(default=1.0, ge=0.01)
    unit_price: float = Field(...)
    discount_amount: Optional[float] = Field(default=None, ge=0.0)
    total_price: float = Field(...)
    category: str = Field(default="General", max_length=50)
    domain: Optional[str] = Field(default=None, max_length=50)
    product_type: Optional[str] = Field(default=None, max_length=50)
    tags: list[str] = Field(default_factory=list)
    volume_ml: Optional[str] = Field(default=None, max_length=50)
    notes: Optional[str] = None

    @field_validator("tags", mode="before")
    @classmethod
    def parse_tags_if_string(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [str(t) for t in parsed]
            except Exception:
                return [t.strip() for t in v.split(",") if t.strip()]
        if isinstance(v, list):
            return [str(t) for t in v]
        return []


class ReceiptItemCreate(ReceiptItemBase):
    pass


class ReceiptItemUpdate(BaseModel):
    category: Optional[str] = Field(default=None, max_length=50)
    domain: Optional[str] = Field(default=None, max_length=50)
    tags: Optional[list[str]] = None
    notes: Optional[str] = None


class ReceiptItemRead(ReceiptItemBase):
    id: int
    receipt_id: int
    inherited_tags: list[str] = Field(default_factory=list)
    created_at: datetime

    class Config:
        from_attributes = True


VALID_RECEIPT_DOCUMENT_TYPES = {"receipt", "payslip"}


def normalize_receipt_document_type(value: Any) -> str:
    """Normalize receipt document_type to 'receipt' or 'payslip'."""
    if value is None:
        return "receipt"
    cleaned = str(value).strip().lower()
    if cleaned not in VALID_RECEIPT_DOCUMENT_TYPES:
        raise ValueError(
            f"Unsupported document_type '{value}'. Must be one of: {', '.join(sorted(VALID_RECEIPT_DOCUMENT_TYPES))}."
        )
    return cleaned


class ReceiptBase(BaseModel):
    document_type: str = Field(default="receipt", max_length=32)
    receipt_date: date
    merchant: str = Field(..., max_length=150)
    total_amount: float = Field(..., ge=0.0)
    currency: str = Field(default="NZD", max_length=3)
    tax_amount: Optional[float] = Field(default=None, ge=0.0)
    order_number: Optional[str] = Field(default=None, max_length=128)
    status: str = Field(default="unlinked", max_length=30)
    notes: Optional[str] = None

    @field_validator("document_type", mode="before")
    @classmethod
    def validate_document_type(cls, v: Any) -> str:
        return normalize_receipt_document_type(v)


class ReceiptCreate(ReceiptBase):
    items: list[ReceiptItemCreate] = []


class ReceiptRead(ReceiptBase):
    id: int
    transaction_id: Optional[int] = None
    source_email_message_id: Optional[str] = None
    image_path: Optional[str] = None
    image_mime_type: Optional[str] = None
    image_filename: Optional[str] = None
    has_image: bool = False
    is_processed: bool = False
    raw_text: Optional[str] = None
    items: list[ReceiptItemRead] = []
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TransactionCandidateRead(BaseModel):
    transaction_id: int
    transaction_date: date
    merchant: str
    raw_description: Optional[str] = None
    amount: float
    currency: str
    account_id: int
    account_name: Optional[str] = None
    original_amount: Optional[float] = None
    original_currency: Optional[str] = None
    estimated_receipt_amount: Optional[float] = None
    confidence: float
    match_reason: str


class ProductStatisticItem(BaseModel):
    item_name: str
    brand: Optional[str] = None
    category: str
    domain: Optional[str] = None
    product_type: Optional[str] = None
    volume_ml: Optional[str] = None
    purchase_count: int
    total_spent: float
    unit_price_latest: float
    first_purchased: date
    last_purchased: date
    avg_days_between_purchases: Optional[float] = None
    estimated_depletion_date: Optional[date] = None


class ProductStatisticsResponse(BaseModel):
    domain: Optional[str] = None
    total_products: int
    total_spend: float
    products: list[ProductStatisticItem]


# --- Runway & Time-Horizon Schemas ---
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


# --- External Connector & Ingestion Schemas ---
class SyncAccountSummary(BaseModel):
    account_id: int
    account_name: str
    institution: str
    currency: str
    balance: Optional[float] = None
    transactions_created: int = 0
    transactions_skipped: int = 0


class SyncResult(BaseModel):
    provider: str
    accounts_synced: int = 0
    transactions_created: int = 0
    transactions_skipped: int = 0
    snapshots_updated: int = 0
    accounts: list[SyncAccountSummary] = Field(default_factory=list)


class DbsAlertPayload(BaseModel):
    """Inbound email alert payload forwarded from Cloudflare Email Worker."""
    subject: str = Field(..., max_length=300)
    text: str = Field(..., min_length=1)
    from_address: Optional[str] = Field(default=None, max_length=200)
    account_name: Optional[str] = Field(default=None, max_length=100)


class DbsAlertResult(BaseModel):
    status: str
    transaction: Optional[TransactionRead] = None
    duplicate: bool = False


VALID_EMAIL_ROUTE_TARGETS = {
    "plutus_dbs_alert",
    "plutus_purchase_receipt",
    "plutus_payslip",
    "plutus_bank_statement",
}


class EmailAttachmentPayload(BaseModel):
    filename: Optional[str] = Field(default=None, max_length=255)
    content_type: Optional[str] = Field(default=None, max_length=128)
    size_bytes: Optional[int] = Field(default=None, ge=0)
    sha256: Optional[str] = Field(default=None, max_length=128)
    content_base64: Optional[str] = None


class InboundEmailDispatchPayload(BaseModel):
    """Unified inbound financial email payload routed from Iris."""

    email_id: Optional[int] = None
    message_id: str = Field(..., min_length=1, max_length=512)
    route_target: str = Field(..., min_length=1, max_length=64)
    from_address: Optional[str] = Field(default=None, max_length=255)
    to_address: Optional[str] = Field(default=None, max_length=255)
    subject: Optional[str] = Field(default="", max_length=500)
    received_at: Optional[datetime] = None
    text_body: Optional[str] = None
    html_excerpt: Optional[str] = None
    raw_excerpt: Optional[str] = None
    jev_verdict: Optional[Any] = None
    attachments: list[EmailAttachmentPayload] = Field(default_factory=list)

    @field_validator("message_id", mode="before")
    @classmethod
    def validate_message_id(cls, v: Any) -> str:
        cleaned = str(v or "").strip()
        if not cleaned:
            raise ValueError("message_id is required.")
        return cleaned

    @field_validator("route_target", mode="before")
    @classmethod
    def validate_route_target(cls, v: Any) -> str:
        cleaned = str(v or "").strip().lower()
        if cleaned not in VALID_EMAIL_ROUTE_TARGETS:
            raise ValueError(
                f"Unsupported route_target '{v}'. Must be one of: {', '.join(sorted(VALID_EMAIL_ROUTE_TARGETS))}."
            )
        return cleaned


class InboundEmailDispatchResult(BaseModel):
    status: str
    route_target: str
    message_id: str
    duplicate: bool = False
    consumer_ref: Optional[str] = None
    transaction_id: Optional[int] = None
    receipt_id: Optional[int] = None
    receipt_ids: list[int] = Field(default_factory=list)
    error_detail: Optional[str] = None


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


from app.services.statement_parser import ParsedTransaction, StatementParseResult
