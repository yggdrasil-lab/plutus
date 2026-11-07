"""Pydantic schemas for Plutus API."""

from datetime import date, datetime
import json
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


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
    has_receipt: bool = False
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


# --- Outlook-style Categorization Rule Schemas ---
class CategorizationRuleBase(BaseModel):
    name: str = Field(..., max_length=100)
    description_pattern: str = Field(..., max_length=200)
    match_type: str = Field(default="contains", description="'contains', 'exact', or 'starts_with'")
    amount_equals: Optional[float] = Field(default=None, ge=0.0)
    target_category: str = Field(..., max_length=50)
    rename_merchant: Optional[str] = Field(default=None, max_length=150)
    priority: int = Field(default=10, ge=1)
    is_active: bool = True


class CategorizationRuleCreate(CategorizationRuleBase):
    apply_retroactively: bool = Field(default=True, description="Apply immediately to matching historical transactions")


class CategorizationRuleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=100)
    description_pattern: Optional[str] = Field(default=None, max_length=200)
    match_type: Optional[str] = None
    amount_equals: Optional[float] = None
    target_category: Optional[str] = Field(default=None, max_length=50)
    rename_merchant: Optional[str] = Field(default=None, max_length=150)
    priority: Optional[int] = None
    is_active: Optional[bool] = None
    apply_retroactively: bool = False


class CategorizationRuleRead(CategorizationRuleBase):
    id: int
    match_count: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# --- Product Catalog & Price History Schemas ---
class PricePoint(BaseModel):
    """Historical unit price record from an itemized receipt."""
    date: date
    merchant: str
    unit_price: float
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
    unit_price: float = Field(..., ge=0.0)
    total_price: float = Field(..., ge=0.0)
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


class ReceiptItemRead(ReceiptItemBase):
    id: int
    receipt_id: int
    created_at: datetime

    class Config:
        from_attributes = True


class ReceiptBase(BaseModel):
    receipt_date: date
    merchant: str = Field(..., max_length=150)
    total_amount: float = Field(..., ge=0.0)
    currency: str = Field(default="NZD", max_length=3)
    tax_amount: Optional[float] = Field(default=None, ge=0.0)
    status: str = Field(default="unlinked", max_length=30)
    notes: Optional[str] = None


class ReceiptCreate(ReceiptBase):
    items: list[ReceiptItemCreate] = []


class ReceiptRead(ReceiptBase):
    id: int
    transaction_id: Optional[int] = None
    image_path: Optional[str] = None
    raw_text: Optional[str] = None
    items: list[ReceiptItemRead] = []
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


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
