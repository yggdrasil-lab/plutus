"""Core constants, exceptions, query inputs, and frozen domain DTOs for Spending Projection."""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from app.models import BudgetRule
from app.services.category_taxonomy import get_root_category

PeriodLiteral = Literal["week", "last_week", "month", "last_month", "annual", "year", "all"]
GroupByLiteral = Literal["leaf", "base"]
FlowLiteral = Literal["inflow", "outflow"]

VALID_PERIODS: tuple[str, ...] = (
    "week",
    "last_week",
    "month",
    "last_month",
    "annual",
    "year",
    "all",
)
WEEKLY_CADENCE_PERIODS: tuple[str, ...] = ("week", "last_week")
MONTHLY_CADENCE_PERIODS: tuple[str, ...] = ("month", "last_month")
BUDGET_CADENCE_PERIODS: tuple[str, ...] = (*WEEKLY_CADENCE_PERIODS, *MONTHLY_CADENCE_PERIODS)
TRANSFER_CATEGORIES: tuple[str, ...] = ("transfers", "transfer")

WEEKS_PER_YEAR = 52.0
MONTHS_PER_YEAR = 12.0
DAYS_PER_YEAR = 365
DEFAULT_WEEKLY_BURN_NZD = 1000.0
MIN_SIGNIFICANT_WEEKLY_BURN_NZD = 1.0

RECURRENCE_WEEKLY_FACTOR: dict[str, float] = {
    "weekly": 1.0,
    "fortnightly": 0.5,
    "monthly": 12.0 / 52.0,
    "quarterly": 4.0 / 52.0,
    "annual": 1.0 / 52.0,
}

RECURRENCE_LOOKBACK_DAYS: dict[str, int] = {
    "weekly": 28,
    "fortnightly": 28,
    "monthly": 35,
    "quarterly": 95,
    "annual": 366,
}


class InvalidWindowError(ValueError):
    """Raised when a ReportingWindow, TransactionQuery, or grouping parameter is invalid."""


@dataclass(frozen=True)
class ReportingWindow:
    """Declarative query window for spending and ledger projections."""

    period: str | None = None
    start_date: date | None = None
    end_date: date | None = None


@dataclass(frozen=True)
class ResolvedWindow:
    """Concrete inclusive date bounds and cadence metadata for a ReportingWindow."""

    period_label: str
    effective_period: str | None
    start_date: date | None
    end_date: date | None
    reference_date: date
    as_of_date: date
    is_weekly_cadence: bool
    is_monthly_cadence: bool
    is_budget_cadence_window: bool
    amortized_recurrences: frozenset[str]


@dataclass(frozen=True)
class TransactionQuery:
    """Declarative query contract for projected or ledger transaction listings."""

    window: ReportingWindow = ReportingWindow()
    category: str | None = None
    tag: str | None = None
    flow: FlowLiteral | None = None
    include_subclasses: bool = False
    include_amortized: bool = False
    account_id: int | None = None
    loan_id: int | None = None
    limit: int = 100
    offset: int = 0


@dataclass(frozen=True)
class ExpenseLine:
    """Canonical NZD outflow allocation produced from a split, receipt item, or parent transaction."""

    category: str
    amount_nzd: float
    effective_tags: tuple[str, ...]
    item_name: str | None = None
    source: Literal["parent", "manual_split", "receipt_item"] = "parent"


@dataclass(frozen=True)
class RecurringCommitmentAllocation:
    """Per-category active recurring commitment allocation normalized to weekly and monthly NZD."""

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
    account_id: int | None = None


@dataclass(frozen=True)
class CategoryProjection:
    """Immutable spending and budget utilization projection for a single category."""

    category: str
    parent_category: str | None
    total_spend: float
    transaction_count: int
    actual_transaction_count: int
    amortized_count: int
    percentage: float
    budget_limit: float | None
    percent_used: float | None
    is_over_budget: bool
    warning_level: str
    is_transfer: bool
    budget_period: str
    annual_limit: float | None


@dataclass(frozen=True)
class TagCategoryProjection:
    """Immutable per-category spending breakdown inside a TagProjection."""

    category: str
    total_spend: float
    transaction_count: int
    percentage: float


@dataclass(frozen=True)
class TagProjection:
    """Immutable spending summary for a single normalized tag within a ReportingWindow."""

    tag: str
    total_spend: float
    transaction_count: int
    first_date: date
    last_date: date
    categories: tuple[TagCategoryProjection, ...]


@dataclass(frozen=True)
class WindowProjection:
    """Immutable spending, income, transfer, category, and tag projection for a ReportingWindow."""

    period: str
    start_date: date | None
    end_date: date | None
    total_spend: float
    total_income: float
    total_transfers: float
    net_savings: float
    categories: tuple[CategoryProjection, ...]
    over_budget_count: int
    tags: tuple[TagProjection, ...] = ()
    total_tagged_spend: float = 0.0


@dataclass(frozen=True)
class ProjectedSplit:
    """Detached-session-safe snapshot of a Manual Split line."""

    id: int
    transaction_id: int
    amount: float
    category: str
    tags: tuple[str, ...] = ()
    loan_id: int | None = None
    loan_name: str | None = None
    loan_type: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class ProjectedTransaction:
    """Detached-session-safe immutable transaction snapshot with optional slice overrides."""

    id: int
    transaction_date: date
    account_id: int
    amount: float
    currency: str
    merchant: str
    category: str
    raw_hash: str
    created_at: datetime
    account_name: str | None = None
    raw_description: str | None = None
    notes: str | None = None
    linked_transaction_id: int | None = None
    linked_account_name: str | None = None
    linked_amount: float | None = None
    linked_currency: str | None = None
    counterpart_account_id: int | None = None
    counterpart_account_name: str | None = None
    counterpart_account_type: str | None = None
    loan_id: int | None = None
    loan_name: str | None = None
    loan_type: str | None = None
    loan_payment_amount: float | None = None
    exchange_rate: float | None = None
    fee_amount: float | None = None
    tags: tuple[str, ...] = ()
    recurrence: str | None = None
    has_receipt: bool = False
    receipt_id: int | None = None
    receipt_document_type: str | None = None
    is_receipt_split: bool = False
    splits: tuple[ProjectedSplit, ...] = ()
    is_split: bool = False
    is_amortized_commitment: bool = False
    amortized_period_amount_nzd: float | None = None
    amortized_cadence: str | None = None
    excluded_from_period_budget: bool = False


@dataclass(frozen=True)
class TransactionProjection:
    """Paginated immutable transaction projection rows and pre-slice total count."""

    items: tuple[ProjectedTransaction, ...]
    total_count: int


ProjectedCashflowItem = ProjectedTransaction
CashflowProjectionResult = TransactionProjection


@dataclass(frozen=True)
class CategoryBurnEstimate:
    """Average weekly burn breakdown and budget limit suggestions for one category."""

    category: str
    parent_category: str | None
    variable_weekly_burn: float
    recurring_weekly_burn: float
    average_weekly_burn: float
    suggested_monthly_limit: float
    suggested_annual_limit: float = 0.0
    budget_period: str = "monthly"
    variable_percentage: float = 0.0
    total_percentage: float = 0.0


@dataclass(frozen=True)
class RecurringCommitmentDetail:
    """Itemized active recurring commitment allocation normalized to weekly and monthly NZD."""

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


@dataclass(frozen=True)
class WeeklyCategoryBurn:
    """Current-week projected NZD spend for one category, reconciled with this_week_spend."""

    category: str
    spend_nzd: float


@dataclass(frozen=True)
class BurnProjection:
    """Immutable trailing weekly burn, category estimate, commitment, and current-week snapshot."""

    as_of_date: date
    lookback_weeks: int
    start_of_week: date
    variable_weekly_burn: float
    recurring_weekly_burn: float
    observed_weekly_burn: float
    effective_weekly_burn: float
    weekly_budget_target: float
    this_week_variable_spend: float
    this_week_spend: float
    this_week_categories: tuple[WeeklyCategoryBurn, ...]
    categories: tuple[CategoryBurnEstimate, ...]
    recurring_commitments: tuple[RecurringCommitmentDetail, ...]


@dataclass(frozen=True)
class _AnnualCategoryMetadata:
    """Resolved taxonomy, active BudgetRules, and Annualized Category sets without 365d sums."""

    resolved: ResolvedWindow
    rollup_base: bool
    parent_map: dict[str, str]
    lower_to_parent: dict[str, str]
    canonical_name_map: dict[str, str]
    raw_budget_rules: dict[str, BudgetRule]
    annual_cats: frozenset[str]
    annual_base_roots: frozenset[str]

    def resolve_category(self, category: str) -> str:
        return get_root_category(category, self.parent_map) if self.rollup_base else category

    def is_smoothed_annual(self, raw_lower: str, effective_lower: str) -> bool:
        if not self.resolved.is_budget_cadence_window:
            return False
        return raw_lower in self.annual_cats or (
            self.rollup_base and effective_lower in self.annual_base_roots
        )


@dataclass(frozen=True)
class _BudgetProjectionContext:
    """Resolved taxonomy, budget rules, and 365-day annual totals for a single projection run."""

    resolved: ResolvedWindow
    rollup_base: bool
    parent_map: dict[str, str]
    canonical_name_map: dict[str, str]
    annual_cats: frozenset[str]
    annual_base_roots: frozenset[str]
    effective_annual_set: frozenset[str]
    effective_budget_rules: dict[str, BudgetRule]
    annual_spend_map: dict[str, float]
    annual_count_map: dict[str, int]

    def resolve_category(self, category: str) -> str:
        return get_root_category(category, self.parent_map) if self.rollup_base else category

    def is_smoothed_annual(self, raw_lower: str, effective_lower: str) -> bool:
        if not self.resolved.is_budget_cadence_window:
            return False
        return raw_lower in self.annual_cats or (
            self.rollup_base and effective_lower in self.annual_base_roots
        )


@dataclass
class _CategoryBucket:
    """Mutable accumulator for one category during a window projection pass."""

    spend: float = 0.0
    count: int = 0
    actual_count: int = 0
    amortized_count: int = 0
    is_transfer: bool = False


@dataclass
class _WindowAccumulator:
    """Mutable ledger totals and category buckets accumulated during project_window."""

    total_spend: float = 0.0
    total_income: float = 0.0
    total_transfers: float = 0.0
    categories: dict[str, _CategoryBucket] = field(default_factory=dict)

    def add_category(
        self,
        category: str,
        amount_nzd: float,
        *,
        is_transfer: bool = False,
        is_amortized: bool = False,
    ) -> None:
        bucket = self.categories.get(category)
        if bucket is None:
            bucket = _CategoryBucket(is_transfer=is_transfer)
            self.categories[category] = bucket
        bucket.spend += amount_nzd
        bucket.count += 1
        if is_amortized:
            bucket.amortized_count += 1
        else:
            bucket.actual_count += 1


@dataclass
class _TagCategoryBucket:
    """Mutable per-category totals under one tag during a window pass."""

    spend: float = 0.0
    tx_ids: set[int] = field(default_factory=set)


@dataclass
class _TagBucket:
    """Mutable tag spending bucket during a window pass."""

    first_date: date
    last_date: date
    spend: float = 0.0
    tx_ids: set[int] = field(default_factory=set)
    categories: dict[str, _TagCategoryBucket] = field(default_factory=dict)


@dataclass
class _TagAccumulator:
    """Mutable tag spending accumulator populated from canonical ExpenseLine entries."""

    total_tagged_spend: float = 0.0
    tags: dict[str, _TagBucket] = field(default_factory=dict)

    def add_line(
        self,
        *,
        tx_id: int,
        tx_date: date,
        category: str,
        amount_nzd: float,
        tags: tuple[str, ...],
    ) -> None:
        if not tags:
            return
        self.total_tagged_spend += amount_nzd
        for tag_key in tags:
            bucket = self.tags.get(tag_key)
            if bucket is None:
                bucket = _TagBucket(first_date=tx_date, last_date=tx_date)
                self.tags[tag_key] = bucket
            bucket.spend += amount_nzd
            bucket.tx_ids.add(tx_id)
            if tx_date < bucket.first_date:
                bucket.first_date = tx_date
            if tx_date > bucket.last_date:
                bucket.last_date = tx_date
            cat_bucket = bucket.categories.setdefault(category, _TagCategoryBucket())
            cat_bucket.spend += amount_nzd
            cat_bucket.tx_ids.add(tx_id)

    def build_projections(self) -> tuple[tuple[TagProjection, ...], float]:
        items: list[TagProjection] = []
        for tag_key, bucket in self.tags.items():
            tag_total = round(bucket.spend, 2)
            cat_items = [
                TagCategoryProjection(
                    category=cat_name,
                    total_spend=round(cat_data.spend, 2),
                    transaction_count=len(cat_data.tx_ids),
                    percentage=(
                        round(cat_data.spend / tag_total * 100.0, 1) if tag_total > 0 else 0.0
                    ),
                )
                for cat_name, cat_data in bucket.categories.items()
            ]
            cat_items.sort(key=lambda c: (-c.total_spend, c.category))
            items.append(
                TagProjection(
                    tag=tag_key,
                    total_spend=tag_total,
                    transaction_count=len(bucket.tx_ids),
                    first_date=bucket.first_date,
                    last_date=bucket.last_date,
                    categories=tuple(cat_items),
                )
            )
        items.sort(key=lambda t: (-t.total_spend, t.tag))
        return tuple(items), round(self.total_tagged_spend, 2)
