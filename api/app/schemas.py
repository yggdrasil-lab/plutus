"""Pydantic schemas for Plutus API."""

from datetime import date, datetime
import json
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


VALID_BUDGET_PERIODS = {"weekly", "monthly", "annual"}


def normalize_budget_period(value: Any) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized not in VALID_BUDGET_PERIODS:
        raise ValueError(f"Unsupported budget_period '{value}'. Must be 'weekly', 'monthly', or 'annual'.")
    return normalized


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
    external_provider: Optional[str] = None
    external_account_id: Optional[str] = None
    last_synced_at: Optional[datetime] = None
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
        if isinstance(v, list):
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
    tags: Optional[list[str]] = None
    recurrence: Optional[str] = Field(default=None, max_length=20)

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
class CategorizationRuleBase(BaseModel):
    name: str = Field(..., max_length=100)
    description_pattern: str = Field(..., max_length=200)
    match_type: str = Field(default="contains", description="'contains', 'exact', or 'starts_with'")
    amount_equals: Optional[float] = Field(default=None, ge=0.0)
    target_category: str = Field(..., max_length=50)
    target_tags: list[str] = Field(default_factory=list)
    target_recurrence: Optional[str] = Field(default=None, max_length=20)
    rename_merchant: Optional[str] = Field(default=None, max_length=150)
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


class CategorizationRuleCreate(CategorizationRuleBase):
    apply_retroactively: bool = Field(default=True, description="Apply immediately to matching historical transactions")


class CategorizationRuleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=100)
    description_pattern: Optional[str] = Field(default=None, max_length=200)
    match_type: Optional[str] = None
    amount_equals: Optional[float] = None
    target_category: Optional[str] = Field(default=None, max_length=50)
    target_tags: Optional[list[str]] = None
    target_recurrence: Optional[str] = Field(default=None, max_length=20)
    rename_merchant: Optional[str] = Field(default=None, max_length=150)
    priority: Optional[int] = None
    is_active: Optional[bool] = None
    apply_retroactively: bool = False

    @field_validator("target_recurrence", mode="before")
    @classmethod
    def validate_target_recurrence_update(cls, v: Any) -> Optional[str]:
        return normalize_recurrence(v)


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
    unit_price: float = Field(...)
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


class LoanBase(BaseModel):
    name: str = Field(..., max_length=120)
    lender: str = Field(..., max_length=120)
    principal_amount: float = Field(..., gt=0.0)
    annual_interest_rate: float = Field(..., ge=0.0, le=1.0)
    term_months: int = Field(..., ge=1, le=600)
    monthly_payment: float = Field(..., gt=0.0)
    start_date: date
    asset_value: Optional[float] = Field(default=None, ge=0.0)
    currency: str = Field(default="NZD", max_length=3)
    notes: Optional[str] = None
    is_active: bool = True


class LoanCreate(LoanBase):
    pass


class LoanUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    lender: Optional[str] = Field(default=None, max_length=120)
    principal_amount: Optional[float] = Field(default=None, gt=0.0)
    annual_interest_rate: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    term_months: Optional[int] = Field(default=None, ge=1, le=600)
    monthly_payment: Optional[float] = Field(default=None, gt=0.0)
    start_date: Optional[date] = None
    asset_value: Optional[float] = Field(default=None, ge=0.0)
    notes: Optional[str] = None
    is_active: Optional[bool] = None


class LoanRead(LoanBase):
    id: int
    payments_made: int
    payments_remaining: int
    payoff_date: date
    next_payment_date: Optional[date] = None
    remaining_principal: float
    principal_paid: float
    remaining_interest: float
    weekly_payment_equivalent: float
    net_worth_impact: float


class LoansSummaryResponse(BaseModel):
    loans: list[LoanRead] = Field(default_factory=list)
    total_liabilities: float
    total_asset_value: float
    net_loan_impact: float
    total_monthly_payment: float
    total_weekly_payment: float
    latest_payoff_date: Optional[date] = None


from app.services.statement_parser import ParsedTransaction, StatementParseResult
