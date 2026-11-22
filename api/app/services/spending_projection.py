"""Deep read-only Spending Projection module for Plutus reporting windows."""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal, Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.models import BudgetRule, Category, Receipt, Transaction, TransactionSplit
from app.services.category_taxonomy import (
    get_category_parent_map,
    get_descendant_category_names,
    get_parent_category,
    get_root_category,
)
from app.services.fx_service import to_nzd_amount
from app.services.rules_engine import normalize_tags, parse_rule_tags

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
    """Raised when a ReportingWindow or projection grouping parameter is invalid."""


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
class WindowProjection:
    """Immutable spending, income, transfer, and category projection for a ReportingWindow."""

    period: str
    start_date: date | None
    end_date: date | None
    total_spend: float
    total_income: float
    total_transfers: float
    net_savings: float
    categories: tuple[CategoryProjection, ...]
    over_budget_count: int


@dataclass(frozen=True)
class ProjectedCashflowItem:
    """Immutable cashflow row aligned to a source Transaction and optional slice overrides."""

    transaction: Transaction
    projected_amount: Optional[float] = None
    projected_category: Optional[str] = None
    projected_tags: Optional[tuple[str, ...]] = None
    projected_notes: Optional[str] = None
    is_split: Optional[bool] = None
    is_receipt_split: bool = False
    excluded_from_period_budget: bool = False
    is_amortized_commitment: bool = False
    amortized_period_amount_nzd: Optional[float] = None
    amortized_cadence: Optional[str] = None


@dataclass(frozen=True)
class CashflowProjectionResult:
    """Paginated cashflow projection rows and pre-slice non-amortized total count."""

    items: tuple[ProjectedCashflowItem, ...]
    total_count: int


@dataclass(frozen=True)
class _AnnualCategoryMetadata:
    """Resolved taxonomy, active BudgetRules, and Annualized Category sets without 365d sums."""

    resolved: ResolvedWindow
    rollup_base: bool
    parent_map: dict[str, str]
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


def weekly_to_monthly(weekly_amount: float) -> float:
    """Convert a weekly NZD amount to its 52/12 monthly equivalent."""
    return round(float(weekly_amount) * WEEKS_PER_YEAR / MONTHS_PER_YEAR, 2)


def weekly_to_annual(weekly_amount: float) -> float:
    """Convert a weekly NZD amount to its 52-week annual equivalent."""
    return round(float(weekly_amount) * WEEKS_PER_YEAR, 2)


def get_period_amortized_recurrences(period: Optional[str]) -> frozenset[str]:
    """Return recurrence keys amortized into category totals for a named period."""
    if period in WEEKLY_CADENCE_PERIODS:
        return frozenset(RECURRENCE_WEEKLY_FACTOR.keys())
    if period in MONTHLY_CADENCE_PERIODS:
        return frozenset(("annual", "quarterly"))
    return frozenset()


def is_recurrence_excluded_from_period(
    period: Optional[str],
    recurrence: Optional[str],
) -> bool:
    """Return True if a raw transaction's recurrence is excluded from direct period sums."""
    rec = (recurrence or "").strip().lower()
    if not rec:
        return False
    if period in WEEKLY_CADENCE_PERIODS:
        return True
    if period in MONTHLY_CADENCE_PERIODS:
        return rec in ("annual", "quarterly", "one-off")
    return False


def resolve_reporting_window(
    window: ReportingWindow,
    *,
    reference_date: date | None = None,
) -> ResolvedWindow:
    """Validate and resolve a ReportingWindow into concrete bounds and cadence flags."""
    ref = reference_date or date.today()
    raw_period = window.period.strip() if isinstance(window.period, str) else window.period
    if raw_period is not None and raw_period not in VALID_PERIODS:
        raise InvalidWindowError(
            "Invalid period. Choose 'week', 'last_week', 'month', 'last_month', 'annual', or 'all'."
        )

    has_custom_bounds = window.start_date is not None or window.end_date is not None
    if has_custom_bounds:
        if window.start_date and window.end_date and window.start_date > window.end_date:
            raise InvalidWindowError("start_date must be on or before end_date.")
        effective_period = None
        start_date, end_date = window.start_date, window.end_date
    elif not raw_period:
        effective_period = None
        start_date, end_date = None, None
    else:
        effective_period = raw_period
        start_date, end_date = _compute_named_period_bounds(effective_period, ref)

    is_weekly = effective_period in WEEKLY_CADENCE_PERIODS
    is_monthly = effective_period in MONTHLY_CADENCE_PERIODS
    return ResolvedWindow(
        period_label=effective_period or "custom",
        effective_period=effective_period,
        start_date=start_date,
        end_date=end_date,
        reference_date=ref,
        as_of_date=end_date or ref,
        is_weekly_cadence=is_weekly,
        is_monthly_cadence=is_monthly,
        is_budget_cadence_window=(is_weekly or is_monthly),
        amortized_recurrences=get_period_amortized_recurrences(effective_period),
    )


def _compute_named_period_bounds(
    period: str,
    ref: date,
) -> tuple[date | None, date | None]:
    """Compute inclusive (start_date, end_date) for a validated named period."""
    if period == "week":
        week_start = ref - timedelta(days=ref.weekday())
        return week_start, week_start + timedelta(days=6)
    if period == "last_week":
        this_monday = ref - timedelta(days=ref.weekday())
        return this_monday - timedelta(days=7), this_monday - timedelta(days=1)
    if period == "month":
        return date(ref.year, ref.month, 1), ref
    if period == "last_month":
        prev_end = date(ref.year, ref.month, 1) - timedelta(days=1)
        return date(prev_end.year, prev_end.month, 1), prev_end
    if period in ("annual", "year"):
        return ref - timedelta(days=DAYS_PER_YEAR), ref
    return None, None


def _is_lent_loan(loan_obj: object) -> bool:
    """Return True when an attached Loan ORM row has loan_type == 'lent'."""
    return loan_obj is not None and (getattr(loan_obj, "loan_type", None) or "").strip().lower() == "lent"


def expand_expense_lines(tx: Transaction) -> list[ExpenseLine]:
    """Expand a transaction into canonical NZD ExpenseLine items (splits -> receipt -> parent)."""
    tx_cat = (tx.category or "General").strip() or "General"
    parent_tags = parse_rule_tags(tx.tags)
    if getattr(tx, "splits", None):
        return _expand_manual_split_lines(tx, tx_cat, parent_tags)
    return _expand_receipt_or_parent_lines(tx, tx_cat, parent_tags)


def _expand_manual_split_lines(
    tx: Transaction,
    tx_cat: str,
    parent_tags: list[str],
) -> list[ExpenseLine]:
    """Expand manual TransactionSplit rows into qualifying NZD ExpenseLine items."""
    lines: list[ExpenseLine] = []
    for s in getattr(tx, "splits", None) or []:
        signed_amt = s.signed_amount()
        if signed_amt >= 0 or _is_lent_loan(getattr(s, "loan", None)):
            continue
        raw_cat = (s.category or tx_cat).strip() or tx_cat
        if raw_cat.lower() in TRANSFER_CATEGORIES:
            continue
        line_nzd = round(to_nzd_amount(abs(signed_amt), tx.currency, tx.exchange_rate), 2)
        line_tags = tuple(normalize_tags([*parent_tags, *parse_rule_tags(s.tags)]))
        lines.append(
            ExpenseLine(
                category=raw_cat,
                amount_nzd=line_nzd,
                effective_tags=line_tags,
                item_name=s.notes,
                source="manual_split",
            )
        )
    return lines


def _scale_receipt_expense_lines(
    tx: Transaction,
    tx_cat: str,
    parent_tags: list[str],
) -> tuple[list[ExpenseLine], int, float]:
    """Scale positive receipt items pro-rata to NZD, returning (non_transfer_lines, positive_item_count, tx_nzd)."""
    tx_nzd = round(to_nzd_amount(abs(float(tx.amount)), tx.currency, tx.exchange_rate), 2)
    receipt = getattr(tx, "receipt", None)
    items = getattr(receipt, "items", None) if receipt is not None else None
    positive_items = [it for it in (items or []) if float(it.total_price) > 0]
    items_sum = sum(float(it.total_price) for it in positive_items)
    if items_sum <= 0 or not positive_items:
        return [], 0, tx_nzd

    scale = tx_nzd / items_sum
    scaled = [
        ExpenseLine(
            category=(it.category or tx_cat).strip() or tx_cat,
            amount_nzd=round(float(it.total_price) * scale, 2),
            effective_tags=tuple(normalize_tags([*parent_tags, *parse_rule_tags(it.tags)])),
            item_name=it.item_name,
            source="receipt_item",
        )
        for it in positive_items
    ]
    delta = round(tx_nzd - sum(line.amount_nzd for line in scaled), 2)
    if delta != 0.0 and scaled:
        max_idx = max(range(len(scaled)), key=lambda i: scaled[i].amount_nzd)
        target = scaled[max_idx]
        scaled[max_idx] = ExpenseLine(
            category=target.category,
            amount_nzd=round(target.amount_nzd + delta, 2),
            effective_tags=target.effective_tags,
            item_name=target.item_name,
            source=target.source,
        )
    non_transfer = [line for line in scaled if line.category.lower() not in TRANSFER_CATEGORIES]
    return non_transfer, len(positive_items), tx_nzd


def _expand_receipt_or_parent_lines(
    tx: Transaction,
    tx_cat: str,
    parent_tags: list[str],
) -> list[ExpenseLine]:
    """Expand an unsplit transaction via receipt line items or parent category in NZD."""
    if float(tx.amount) >= 0 or _is_lent_loan(getattr(tx, "loan", None)):
        return []
    if tx_cat.lower() in TRANSFER_CATEGORIES:
        return []

    scaled, positive_count, tx_nzd = _scale_receipt_expense_lines(tx, tx_cat, parent_tags)
    if positive_count > 0:
        return scaled
    return [
        ExpenseLine(
            category=tx_cat,
            amount_nzd=tx_nzd,
            effective_tags=tuple(parent_tags),
            source="parent",
        )
    ]


def expand_expense_buckets(tx: Transaction) -> list[tuple[str, float]]:
    """Collapse a transaction's ExpenseLine items into per-category (category, amount_nzd) buckets."""
    grouped: dict[str, float] = {}
    canonical: dict[str, str] = {}
    for line in expand_expense_lines(tx):
        key = line.category.lower()
        canonical.setdefault(key, line.category)
        grouped[key] = grouped.get(key, 0.0) + line.amount_nzd
    return [(canonical[key], round(amt, 2)) for key, amt in grouped.items()]


def _transaction_eager_options(
    *,
    include_receipt_items: bool = True,
    eager_hydrate: bool = False,
):
    """Return consistent SQLAlchemy joinedload options for projection and optional hydration."""
    receipt_load = (
        joinedload(Transaction.receipt).joinedload(Receipt.items)
        if include_receipt_items
        else joinedload(Transaction.receipt)
    )
    options = [
        receipt_load,
        joinedload(Transaction.loan),
        joinedload(Transaction.splits).joinedload(TransactionSplit.loan),
    ]
    if eager_hydrate:
        options.extend(
            [
                joinedload(Transaction.account),
                joinedload(Transaction.counterpart_account),
                joinedload(Transaction.linked_transaction).joinedload(Transaction.account),
            ]
        )
    return tuple(options)


def _query_recurring_candidate_transactions(
    db: Session,
    as_of: date,
    *,
    eager_hydrate: bool = False,
) -> list[Transaction]:
    """Fetch candidate recurring outflow transactions within the maximum 366-day lookback."""
    max_lookback = as_of - timedelta(days=366)
    stmt = (
        select(Transaction)
        .options(
            *_transaction_eager_options(
                include_receipt_items=False,
                eager_hydrate=eager_hydrate,
            )
        )
        .where(
            Transaction.transaction_date >= max_lookback,
            Transaction.transaction_date <= as_of,
            or_(
                Transaction.amount < 0,
                Transaction.splits.any(TransactionSplit.amount < 0),
            ),
            Transaction.recurrence.in_(tuple(RECURRENCE_WEEKLY_FACTOR.keys())),
            or_(
                func.lower(Transaction.category).not_in(TRANSFER_CATEGORIES),
                Transaction.splits.any(),
            ),
        )
        .order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
    )
    return list(db.scalars(stmt).unique().all())


def _iter_active_recurring_allocations(
    db: Session,
    as_of: date,
    *,
    eager_hydrate: bool = False,
) -> list[tuple[Transaction, RecurringCommitmentAllocation, bool]]:
    """Return (source_tx, allocation, is_split_slice) for active recurring commitments."""
    rows = _query_recurring_candidate_transactions(db, as_of, eager_hydrate=eager_hydrate)
    seen: set[tuple[str, str]] = set()
    entries: list[tuple[Transaction, RecurringCommitmentAllocation, bool]] = []
    for tx in rows:
        rec = (tx.recurrence or "").strip().lower()
        factor = RECURRENCE_WEEKLY_FACTOR.get(rec)
        if not factor or (as_of - tx.transaction_date).days > RECURRENCE_LOOKBACK_DAYS[rec]:
            continue
        tx_slices = _extract_recurring_outflow_slices(tx)
        if not tx_slices:
            continue
        key = ((tx.merchant or "").strip().lower(), rec)
        if key in seen:
            continue
        seen.add(key)
        is_split_slice = bool(getattr(tx, "splits", None))
        for alloc in _build_tx_commitment_allocations(tx, rec, factor, tx_slices):
            entries.append((tx, alloc, is_split_slice))
    return entries


def get_active_recurring_commitment_allocations(
    db: Session,
    as_of: date,
) -> list[RecurringCommitmentAllocation]:
    """Return per-category active recurring commitment allocations within lookback windows."""
    return [alloc for _, alloc, _ in _iter_active_recurring_allocations(db, as_of)]


def _build_tx_commitment_allocations(
    tx: Transaction,
    rec: str,
    factor: float,
    tx_slices: list[tuple[str, float]],
) -> list[RecurringCommitmentAllocation]:
    """Build RecurringCommitmentAllocation entries for each category slice of a recurring tx."""
    merchant_name = (tx.merchant or "").strip() or "Unnamed commitment"
    currency_code = (tx.currency or "NZD").strip().upper()
    result: list[RecurringCommitmentAllocation] = []
    for cat_name, orig_amt in tx_slices:
        amt_nzd = to_nzd_amount(orig_amt, tx.currency, tx.exchange_rate)
        wk_nzd = round(amt_nzd * factor, 2)
        result.append(
            RecurringCommitmentAllocation(
                transaction_id=tx.id,
                merchant=merchant_name,
                category=cat_name,
                recurrence=rec,
                last_charged_date=tx.transaction_date,
                original_amount=round(orig_amt, 2),
                currency=currency_code,
                amount_nzd=round(amt_nzd, 2),
                weekly_nzd=wk_nzd,
                monthly_nzd=weekly_to_monthly(wk_nzd),
                account_id=tx.account_id,
            )
        )
    return result


def _extract_recurring_outflow_slices(tx: Transaction) -> list[tuple[str, float]]:
    """Extract qualifying (category, positive_original_amount) slices for a recurring transaction."""
    tx_cat = (tx.category or "General").strip() or "General"
    splits = getattr(tx, "splits", None) or []
    if splits:
        grouped_orig: dict[str, float] = {}
        canonical_cat: dict[str, str] = {}
        for s in splits:
            signed_amt = s.signed_amount()
            if signed_amt >= 0 or _is_lent_loan(getattr(s, "loan", None)):
                continue
            s_cat = (s.category or tx_cat).strip() or tx_cat
            s_lower = s_cat.lower()
            if s_lower in TRANSFER_CATEGORIES:
                continue
            canonical_cat.setdefault(s_lower, s_cat)
            grouped_orig[s_lower] = grouped_orig.get(s_lower, 0.0) + abs(signed_amt)
        return [(canonical_cat[k], round(amt, 2)) for k, amt in grouped_orig.items()]

    if float(tx.amount) >= 0 or _is_lent_loan(getattr(tx, "loan", None)):
        return []
    if tx_cat.lower() in TRANSFER_CATEGORIES:
        return []
    return [(tx_cat, round(abs(float(tx.amount)), 2))]


def _compute_annual_category_totals(
    db: Session,
    as_of: date,
    categories: set[str],
) -> tuple[dict[str, float], dict[str, int]]:
    """Single-pass 365-day query returning (spend_map, count_map) for Annualized Categories."""
    target_lower = {c.strip().lower() for c in categories if c and c.strip()}
    if not target_lower:
        return {}, {}

    start_365 = as_of - timedelta(days=DAYS_PER_YEAR)
    txs = db.scalars(
        select(Transaction)
        .options(
            joinedload(Transaction.receipt).joinedload(Receipt.items),
            joinedload(Transaction.loan),
            joinedload(Transaction.splits).joinedload(TransactionSplit.loan),
        )
        .where(
            Transaction.transaction_date >= start_365,
            Transaction.transaction_date <= as_of,
            or_(
                Transaction.amount < 0,
                Transaction.splits.any(TransactionSplit.amount < 0),
            ),
            or_(
                func.lower(Transaction.category).not_in(TRANSFER_CATEGORIES),
                Transaction.splits.any(),
            ),
        )
    ).unique().all()

    spend_map: dict[str, float] = {}
    count_map: dict[str, int] = {}
    for tx in txs:
        for cat_name, amt_nzd in expand_expense_buckets(tx):
            key = cat_name.lower()
            if key in target_lower:
                spend_map[key] = spend_map.get(key, 0.0) + amt_nzd
                count_map[key] = count_map.get(key, 0) + 1
    return spend_map, count_map


def _rollup_budget_rules_to_base(
    raw_budget_rules: dict[str, BudgetRule],
    parent_map: dict[str, str],
    canonical_name_map: dict[str, str],
    annual_base_roots: set[str],
) -> dict[str, BudgetRule]:
    """Aggregate child and root BudgetRules under each base class for group_by=base."""
    grouped_rules: dict[str, list[BudgetRule]] = {}
    grouped_names: dict[str, str] = {}
    for rule in raw_budget_rules.values():
        root_name = get_root_category(rule.category, parent_map)
        root_lower = root_name.lower()
        grouped_names[root_lower] = canonical_name_map.get(root_lower) or root_name
        grouped_rules.setdefault(root_lower, []).append(rule)

    rolled: dict[str, BudgetRule] = {}
    for root_lower, rules_list in grouped_rules.items():
        root_rule = next(
            (r for r in rules_list if r.category.strip().lower() == root_lower),
            None,
        )
        if root_lower in annual_base_roots:
            resolved_period = "annual"
        elif root_rule is not None:
            resolved_period = (root_rule.budget_period or "monthly").strip().lower()
        else:
            child_periods = {(r.budget_period or "monthly").strip().lower() for r in rules_list}
            resolved_period = next(iter(child_periods)) if len(child_periods) == 1 else "monthly"

        rolled[root_lower] = BudgetRule(
            category=grouped_names[root_lower],
            weekly_limit=round(sum(float(r.weekly_limit) for r in rules_list), 2),
            monthly_limit=round(sum(float(r.monthly_limit) for r in rules_list), 2),
            budget_period=resolved_period,
            is_active=True,
        )
    return rolled


def _build_canonical_name_map(
    raw_budget_rules: dict[str, BudgetRule],
    parent_map: dict[str, str],
) -> dict[str, str]:
    """Build lowercase-to-canonical display category map from rules and taxonomy."""
    names: dict[str, str] = {
        str(r.category).strip().lower(): str(r.category).strip()
        for r in raw_budget_rules.values()
        if r.category and str(r.category).strip()
    }
    for child_name, parent_name in parent_map.items():
        names.setdefault(child_name.lower(), child_name)
        names.setdefault(parent_name.lower(), parent_name)
    return names


def _apply_category_annual_flags(
    db: Session,
    annual_cats: set[str],
    canonical_name_map: dict[str, str],
) -> None:
    """Populate canonical category names and annual amortization flags from Category rows."""
    for db_cat in db.scalars(select(Category)).all():
        cat_name = (db_cat.name or "").strip()
        if not cat_name:
            continue
        cat_lower = cat_name.lower()
        canonical_name_map.setdefault(cat_lower, cat_name)
        if db_cat.is_active and (db_cat.amortize_period or "").strip().lower() == "annual":
            annual_cats.add(cat_lower)


def _load_annual_metadata(
    db: Session,
    resolved: ResolvedWindow,
    *,
    rollup_base: bool,
) -> _AnnualCategoryMetadata:
    """Load category taxonomy, active budget rules, and Annualized Category sets."""
    parent_map = get_category_parent_map(db)
    active_rules: list[BudgetRule] = list(
        db.scalars(select(BudgetRule).where(BudgetRule.is_active == True)).all()
    )
    raw_budget_rules: dict[str, BudgetRule] = {
        str(r.category).lower(): r for r in active_rules if r.category
    }
    annual_cats: set[str] = {
        cat_lower
        for cat_lower, rule in raw_budget_rules.items()
        if (rule.budget_period or "monthly").strip().lower() == "annual"
    }
    canonical_name_map = _build_canonical_name_map(raw_budget_rules, parent_map)
    _apply_category_annual_flags(db, annual_cats, canonical_name_map)

    annual_base_roots: set[str] = {
        c
        for c in annual_cats
        if get_parent_category(canonical_name_map.get(c) or c, parent_map) is None
    }
    return _AnnualCategoryMetadata(
        resolved=resolved,
        rollup_base=rollup_base,
        parent_map=parent_map,
        canonical_name_map=canonical_name_map,
        raw_budget_rules=raw_budget_rules,
        annual_cats=frozenset(annual_cats),
        annual_base_roots=frozenset(annual_base_roots),
    )


def _load_budget_context(
    db: Session,
    resolved: ResolvedWindow,
    *,
    rollup_base: bool,
) -> _BudgetProjectionContext:
    """Load category taxonomy, active budget rules, and 365-day annual category totals."""
    meta = _load_annual_metadata(db, resolved, rollup_base=rollup_base)
    return _finalize_budget_context(
        db,
        resolved,
        rollup_base=rollup_base,
        parent_map=meta.parent_map,
        canonical_name_map=meta.canonical_name_map,
        raw_budget_rules=meta.raw_budget_rules,
        annual_cats=set(meta.annual_cats),
        annual_base_roots=set(meta.annual_base_roots),
    )


def _finalize_budget_context(
    db: Session,
    resolved: ResolvedWindow,
    *,
    rollup_base: bool,
    parent_map: dict[str, str],
    canonical_name_map: dict[str, str],
    raw_budget_rules: dict[str, BudgetRule],
    annual_cats: set[str],
    annual_base_roots: set[str],
) -> _BudgetProjectionContext:
    """Compute 365-day annual totals and optional base-class rollups for _BudgetProjectionContext."""
    fetch_annual_cats = set(annual_cats)
    if rollup_base:
        for root_lower in annual_base_roots:
            fetch_annual_cats.update(get_descendant_category_names(root_lower, parent_map))

    if resolved.is_budget_cadence_window:
        raw_spend, raw_count = _compute_annual_category_totals(
            db, resolved.as_of_date, fetch_annual_cats
        )
    else:
        raw_spend, raw_count = {}, {}

    if not rollup_base:
        return _BudgetProjectionContext(
            resolved=resolved,
            rollup_base=False,
            parent_map=parent_map,
            canonical_name_map=canonical_name_map,
            annual_cats=frozenset(annual_cats),
            annual_base_roots=frozenset(annual_base_roots),
            effective_annual_set=frozenset(annual_cats),
            effective_budget_rules=raw_budget_rules,
            annual_spend_map=raw_spend,
            annual_count_map=raw_count,
        )

    annual_spend_map: dict[str, float] = {}
    annual_count_map: dict[str, int] = {}
    for raw_cat_lower, spend_val in raw_spend.items():
        display = canonical_name_map.get(raw_cat_lower) or raw_cat_lower
        root_lower = get_root_category(display, parent_map).lower()
        target_key = root_lower if root_lower in annual_base_roots else raw_cat_lower
        annual_spend_map[target_key] = annual_spend_map.get(target_key, 0.0) + spend_val
    for raw_cat_lower, cnt_val in raw_count.items():
        display = canonical_name_map.get(raw_cat_lower) or raw_cat_lower
        root_lower = get_root_category(display, parent_map).lower()
        target_key = root_lower if root_lower in annual_base_roots else raw_cat_lower
        annual_count_map[target_key] = annual_count_map.get(target_key, 0) + cnt_val

    return _BudgetProjectionContext(
        resolved=resolved,
        rollup_base=True,
        parent_map=parent_map,
        canonical_name_map=canonical_name_map,
        annual_cats=frozenset(annual_cats),
        annual_base_roots=frozenset(annual_base_roots),
        effective_annual_set=frozenset(annual_base_roots),
        effective_budget_rules=_rollup_budget_rules_to_base(
            raw_budget_rules, parent_map, canonical_name_map, annual_base_roots
        ),
        annual_spend_map=annual_spend_map,
        annual_count_map=annual_count_map,
    )


def _query_window_transactions(
    db: Session,
    resolved: ResolvedWindow,
    *,
    account_id: Optional[int] = None,
    loan_id: Optional[int] = None,
    eager_hydrate: bool = False,
) -> list[Transaction]:
    """Query transactions within the resolved ReportingWindow bounds with eager relations."""
    stmt = select(Transaction).options(
        *_transaction_eager_options(include_receipt_items=True, eager_hydrate=eager_hydrate)
    )
    if account_id is not None:
        stmt = stmt.where(
            or_(
                Transaction.account_id == account_id,
                and_(
                    Transaction.counterpart_account_id == account_id,
                    Transaction.linked_transaction_id.is_(None),
                ),
            )
        )
    if loan_id is not None:
        stmt = stmt.where(
            or_(
                Transaction.loan_id == loan_id,
                Transaction.splits.any(TransactionSplit.loan_id == loan_id),
            )
        )
    if resolved.start_date is not None:
        stmt = stmt.where(Transaction.transaction_date >= resolved.start_date)
    if resolved.end_date is not None:
        stmt = stmt.where(Transaction.transaction_date <= resolved.end_date)
    stmt = stmt.order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
    return list(db.scalars(stmt).unique().all())


def _accumulate_window_transaction(
    tx: Transaction,
    ctx: _BudgetProjectionContext,
    acc: _WindowAccumulator,
) -> None:
    """Accumulate one transaction's spend, income, and transfers into the window accumulator."""
    if getattr(tx, "splits", None):
        _accumulate_split_transaction(tx, ctx, acc)
    else:
        _accumulate_unsplit_transaction(tx, ctx, acc)


def _accumulate_split_transaction(
    tx: Transaction,
    ctx: _BudgetProjectionContext,
    acc: _WindowAccumulator,
) -> None:
    """Accumulate a manual-split transaction into transfers, income, and category spend."""
    cat = (tx.category or "General").strip() or "General"
    recurrence_excluded = is_recurrence_excluded_from_period(
        ctx.resolved.effective_period, tx.recurrence
    )
    for s in getattr(tx, "splits", None) or []:
        signed_amt = s.signed_amount()
        s_cat = (s.category or cat).strip() or "General"
        s_lower = s_cat.lower()
        s_nzd = round(to_nzd_amount(abs(signed_amt), tx.currency, tx.exchange_rate), 2)
        if s_lower in TRANSFER_CATEGORIES:
            acc.total_transfers += s_nzd
            acc.add_category("Transfers", s_nzd, is_transfer=True)
            continue
        if _is_lent_loan(getattr(s, "loan", None)):
            continue
        if signed_amt < 0 and not recurrence_excluded:
            effective_cat = ctx.resolve_category(s_cat)
            if not ctx.is_smoothed_annual(s_lower, effective_cat.lower()):
                acc.total_spend += s_nzd
                acc.add_category(effective_cat, s_nzd)
        elif signed_amt > 0:
            acc.total_income += s_nzd


def _accumulate_unsplit_transaction(
    tx: Transaction,
    ctx: _BudgetProjectionContext,
    acc: _WindowAccumulator,
) -> None:
    """Accumulate an unsplit transaction into transfers, income, or receipt-expanded spend."""
    amt = float(tx.amount)
    cat = (tx.category or "General").strip() or "General"
    amt_nzd = to_nzd_amount(amt, tx.currency, tx.exchange_rate)
    if cat.lower() in TRANSFER_CATEGORIES:
        acc.total_transfers += amt_nzd
        acc.add_category("Transfers", amt_nzd, is_transfer=True)
        return

    is_lent = _is_lent_loan(getattr(tx, "loan", None))
    if amt < 0:
        if is_lent or is_recurrence_excluded_from_period(
            ctx.resolved.effective_period, tx.recurrence
        ):
            return
        for item_cat, item_nzd in expand_expense_buckets(tx):
            effective_cat = ctx.resolve_category(item_cat)
            if not ctx.is_smoothed_annual(item_cat.lower(), effective_cat.lower()):
                acc.total_spend += item_nzd
                acc.add_category(effective_cat, item_nzd)
    elif amt > 0:
        acc.total_income += _resolve_unsplit_inflow_nzd(tx, amt_nzd, is_lent=is_lent)


def _resolve_unsplit_inflow_nzd(tx: Transaction, amt_nzd: float, *, is_lent: bool) -> float:
    """Compute net income NZD for a positive unsplit transaction, excluding lent principal repayment."""
    if not is_lent:
        return amt_nzd
    if tx.loan_payment_amount is None:
        return 0.0
    tx_currency = (tx.currency or "NZD").strip().upper()
    repayment_nzd = (
        to_nzd_amount(float(tx.loan_payment_amount), tx.currency, tx.exchange_rate)
        if tx_currency != "NZD"
        else float(tx.loan_payment_amount)
    )
    return max(0.0, amt_nzd - repayment_nzd)


def _inject_amortized_recurrences(
    db: Session,
    ctx: _BudgetProjectionContext,
    acc: _WindowAccumulator,
) -> None:
    """Inject amortized recurring commitments into period category spend for weekly/monthly windows."""
    if not ctx.resolved.amortized_recurrences:
        return
    for alloc in get_active_recurring_commitment_allocations(db, ctx.resolved.as_of_date):
        if alloc.recurrence not in ctx.resolved.amortized_recurrences:
            continue
        target_cat = ctx.resolve_category(alloc.category)
        if ctx.is_smoothed_annual(alloc.category.lower(), target_cat.lower()):
            continue
        amortized_nzd = (
            alloc.monthly_nzd if ctx.resolved.is_monthly_cadence else alloc.weekly_nzd
        )
        acc.total_spend += amortized_nzd
        acc.add_category(target_cat, amortized_nzd, is_amortized=True)


def _inject_annual_category_smoothing(
    ctx: _BudgetProjectionContext,
    acc: _WindowAccumulator,
) -> None:
    """Inject 365-day smoothed weekly/monthly equivalents for Annualized Categories."""
    if not ctx.resolved.is_budget_cadence_window:
        return
    divisor = MONTHS_PER_YEAR if ctx.resolved.is_monthly_cadence else WEEKS_PER_YEAR
    if ctx.rollup_base:
        annual_keys = sorted(
            set(ctx.annual_base_roots)
            | {
                c
                for c in ctx.annual_cats
                if get_root_category(ctx.canonical_name_map.get(c) or c, ctx.parent_map).lower()
                not in ctx.annual_base_roots
            }
        )
    else:
        annual_keys = sorted(ctx.annual_cats)

    for cat_lower in annual_keys:
        annual_total_nzd = ctx.annual_spend_map.get(cat_lower, 0.0)
        if annual_total_nzd <= 0:
            continue
        raw_display = ctx.canonical_name_map.get(cat_lower) or cat_lower.title()
        target_display = ctx.resolve_category(raw_display)
        period_equiv = round(annual_total_nzd / divisor, 2)
        acc.total_spend += period_equiv
        acc.add_category(target_display, period_equiv, is_amortized=True)


def _build_category_projection(
    category: str,
    bucket: _CategoryBucket,
    total_spend: float,
    ctx: _BudgetProjectionContext,
) -> CategoryProjection:
    """Build an immutable CategoryProjection with budget cadence utilization and warnings."""
    cat_lower = category.lower()
    cat_spend = round(bucket.spend, 2)
    rule = ctx.effective_budget_rules.get(cat_lower)
    rule_period = (rule.budget_period or "monthly").strip().lower() if rule else "monthly"
    is_annual_rule = bool(
        not bucket.is_transfer
        and (cat_lower in ctx.effective_annual_set or (rule is not None and rule_period == "annual"))
    )
    use_smoothing = is_annual_rule and ctx.resolved.is_budget_cadence_window
    annual_spend = ctx.annual_spend_map.get(cat_lower)
    annual_count = ctx.annual_count_map.get(cat_lower, 0)

    display_spend = (
        round(annual_spend, 2)
        if (use_smoothing and annual_spend is not None)
        else (0.0 if (use_smoothing and bucket.count == 0) else cat_spend)
    )
    display_count = annual_count if use_smoothing else bucket.count
    display_actual = display_count if use_smoothing else bucket.actual_count
    display_amortized = 0 if use_smoothing else bucket.amortized_count
    pct = (
        round((cat_spend / total_spend * 100), 1)
        if (total_spend > 0 and not bucket.is_transfer)
        else 0.0
    )
    budget_limit, annual_limit, percent_used, is_over, warning = _evaluate_budget_guardrail(
        display_spend, rule, ctx.resolved, is_annual_rule=is_annual_rule, is_transfer=bucket.is_transfer
    )
    resolved_period = (
        "annual"
        if is_annual_rule
        else (rule_period if (rule and not bucket.is_transfer) else "monthly")
    )
    parent_cat = None if ctx.rollup_base else get_parent_category(category, ctx.parent_map)
    return CategoryProjection(
        category=category,
        parent_category=parent_cat,
        total_spend=display_spend,
        transaction_count=display_count,
        actual_transaction_count=display_actual,
        amortized_count=display_amortized,
        percentage=pct,
        budget_limit=budget_limit,
        percent_used=percent_used,
        is_over_budget=is_over,
        warning_level=warning,
        is_transfer=bucket.is_transfer,
        budget_period=resolved_period,
        annual_limit=annual_limit,
    )


def _evaluate_budget_guardrail(
    display_spend: float,
    rule: Optional[BudgetRule],
    resolved: ResolvedWindow,
    *,
    is_annual_rule: bool,
    is_transfer: bool,
) -> tuple[Optional[float], Optional[float], Optional[float], bool, str]:
    """Compute (budget_limit, annual_limit, percent_used, is_over_budget, warning_level)."""
    if not rule or is_transfer:
        return None, None, None, False, "normal"
    if is_annual_rule:
        annual_limit_val: Optional[float] = weekly_to_annual(rule.weekly_limit)
        budget_limit: Optional[float] = annual_limit_val
    else:
        annual_limit_val = None
        budget_limit = float(
            rule.weekly_limit if resolved.is_weekly_cadence else rule.monthly_limit
        )
    if not budget_limit or budget_limit <= 0:
        return budget_limit, annual_limit_val, None, False, "normal"
    percent_used = round((display_spend / budget_limit * 100), 1)
    is_over = display_spend > budget_limit
    warning = "danger" if is_over else ("warning" if percent_used >= 80.0 else "normal")
    return budget_limit, annual_limit_val, percent_used, is_over, warning


def _build_all_category_projections(
    acc: _WindowAccumulator,
    total_spend: float,
    ctx: _BudgetProjectionContext,
) -> tuple[CategoryProjection, ...]:
    """Build sorted CategoryProjection rows across active spend buckets and zero-spend BudgetRules."""
    reports: list[CategoryProjection] = []
    processed_lower: set[str] = set()
    for cat, bucket in acc.categories.items():
        processed_lower.add(cat.lower())
        reports.append(_build_category_projection(cat, bucket, total_spend, ctx))
    for cat_lower, rule in ctx.effective_budget_rules.items():
        if cat_lower not in processed_lower:
            reports.append(
                _build_category_projection(rule.category, _CategoryBucket(), total_spend, ctx)
            )
    reports.sort(key=lambda r: (r.is_transfer, -r.total_spend, r.category))
    return tuple(reports)


def project_window(
    db: Session,
    window: ReportingWindow,
    *,
    group_by: GroupByLiteral = "leaf",
    reference_date: date | None = None,
) -> WindowProjection:
    """Project spending, income, transfers, and category budget utilization for a ReportingWindow."""
    if group_by not in ("leaf", "base"):
        raise InvalidWindowError("group_by must be 'leaf' or 'base'")
    resolved = resolve_reporting_window(window, reference_date=reference_date)
    ctx = _load_budget_context(db, resolved, rollup_base=(group_by == "base"))
    acc = _WindowAccumulator()
    for tx in _query_window_transactions(db, resolved):
        _accumulate_window_transaction(tx, ctx, acc)
    _inject_amortized_recurrences(db, ctx, acc)
    _inject_annual_category_smoothing(ctx, acc)

    total_spend = round(acc.total_spend, 2)
    total_income = round(acc.total_income, 2)
    total_transfers = round(acc.total_transfers, 2)
    categories = _build_all_category_projections(acc, total_spend, ctx)
    over_budget_count = sum(1 for r in categories if r.is_over_budget and not r.is_transfer)
    return WindowProjection(
        period=resolved.period_label,
        start_date=resolved.start_date,
        end_date=resolved.end_date,
        total_spend=total_spend,
        total_income=total_income,
        total_transfers=total_transfers,
        net_savings=round(total_income - total_spend, 2),
        categories=categories,
        over_budget_count=over_budget_count,
    )


def _matches_account_filter(tx: Transaction, account_id: Optional[int]) -> bool:
    """Return True when transaction belongs to account_id directly or as unlinked counterpart."""
    if account_id is None:
        return True
    return tx.account_id == account_id or (
        tx.counterpart_account_id == account_id and tx.linked_transaction_id is None
    )


def _matches_loan_filter(tx: Transaction, loan_id: Optional[int]) -> bool:
    """Return True when transaction or any of its splits links to loan_id."""
    if loan_id is None:
        return True
    if tx.loan_id == loan_id:
        return True
    return any(s.loan_id == loan_id for s in (getattr(tx, "splits", None) or ()))


def _matches_category_filter(
    cat: str,
    target_cat: Optional[str],
    include_subclasses: bool,
    parent_map: dict[str, str],
) -> bool:
    """Return True when cat matches target_cat directly or via Root Category rollup."""
    if not target_cat:
        return True
    effective = (
        get_root_category(cat, parent_map).lower()
        if include_subclasses
        else cat.strip().lower()
    )
    return effective == target_cat


def _nzd_to_tx_currency(amount_nzd: float, tx: Transaction) -> float:
    """Convert a positive NZD amount back to the parent transaction's native currency."""
    fx_unit = to_nzd_amount(1.0, tx.currency, tx.exchange_rate)
    return round(amount_nzd / fx_unit, 2) if fx_unit > 0 else round(amount_nzd, 2)


def _resolve_projected_category(
    tx_cat: str,
    matched_cats: set[str],
    category: Optional[str],
    target_cat: Optional[str],
    include_subclasses: bool,
) -> str:
    """Resolve display category for a projected split or receipt slice."""
    if target_cat and include_subclasses:
        return category.strip() if category else tx_cat
    if len(matched_cats) == 1:
        return next(iter(matched_cats))
    if tx_cat.lower() in TRANSFER_CATEGORIES and matched_cats:
        return sorted(matched_cats)[0]
    return tx_cat


def _build_projected_split_item(
    tx: Transaction,
    tx_cat: str,
    parent_tags: tuple[str, ...],
    matched: list[tuple[TransactionSplit, str, tuple[str, ...], float]],
    *,
    category: Optional[str],
    target_cat: Optional[str],
    include_subclasses: bool,
    excluded: bool = False,
) -> ProjectedCashflowItem:
    """Build a ProjectedCashflowItem from matching Manual Split lines on one transaction."""
    matched_orig = round(sum(amt for _, _, _, amt in matched), 2)
    total_orig = round(sum(s.signed_amount() for s in tx.splits), 2)
    matched_cat = _resolve_projected_category(
        tx_cat, {c for _, c, _, _ in matched}, category, target_cat, include_subclasses
    )
    merged_tags = tuple(normalize_tags(t for _, _, tags, _ in matched for t in tags))
    is_partial = len(matched) < len(tx.splits) or abs(matched_orig - total_orig) > 0.01
    if is_partial or (target_cat and matched_cat.lower() != tx_cat.lower()):
        split_notes = ", ".join(s.notes for s, _, _, _ in matched if s.notes)
        fallback = f"Split of {tx.currency} {abs(float(tx.amount)):.2f}" if is_partial else tx.notes
        return ProjectedCashflowItem(
            transaction=tx,
            projected_amount=matched_orig,
            projected_category=matched_cat,
            projected_tags=merged_tags,
            projected_notes=split_notes or fallback,
            is_split=True,
            excluded_from_period_budget=excluded,
        )
    resolved_cat = matched_cat if matched_cat != tx_cat else None
    return ProjectedCashflowItem(
        transaction=tx,
        projected_category=resolved_cat,
        projected_tags=merged_tags or parent_tags,
        is_split=len(tx.splits) >= 2,
        excluded_from_period_budget=excluded,
    )


def _project_inflow_splits(
    tx: Transaction,
    tx_cat: str,
    parent_tags: tuple[str, ...],
    *,
    category: Optional[str],
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
    parent_map: dict[str, str],
) -> list[ProjectedCashflowItem]:
    """Project positive non-Transfer, non-lent Manual Split lines for flow='inflow'."""
    matched: list[tuple[TransactionSplit, str, tuple[str, ...], float]] = []
    for s in tx.splits:
        signed_amt = s.signed_amount()
        if signed_amt <= 0 or _is_lent_loan(getattr(s, "loan", None)):
            continue
        s_cat = (s.category or tx_cat).strip() or "General"
        if s_cat.lower() in TRANSFER_CATEGORIES:
            continue
        s_tags = tuple(normalize_tags([*parent_tags, *parse_rule_tags(s.tags)]))
        if target_tag and target_tag not in s_tags:
            continue
        if not _matches_category_filter(s_cat, target_cat, include_subclasses, parent_map):
            continue
        matched.append((s, s_cat, s_tags, signed_amt))
    if not matched:
        return []
    return [
        _build_projected_split_item(
            tx,
            tx_cat,
            parent_tags,
            matched,
            category=category,
            target_cat=target_cat,
            include_subclasses=include_subclasses,
        )
    ]


def _project_unsplit_inflow(
    tx: Transaction,
    tx_cat: str,
    parent_tags: tuple[str, ...],
    *,
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
    parent_map: dict[str, str],
) -> list[ProjectedCashflowItem]:
    """Project an unsplit positive Reporting Currency inflow excluding Transfers and lent principal."""
    amt = float(tx.amount)
    if amt <= 0 or tx_cat.lower() in TRANSFER_CATEGORIES:
        return []
    is_lent = _is_lent_loan(getattr(tx, "loan", None))
    amt_nzd = to_nzd_amount(amt, tx.currency, tx.exchange_rate)
    net_nzd = _resolve_unsplit_inflow_nzd(tx, amt_nzd, is_lent=is_lent)
    if net_nzd <= 0:
        return []
    if target_tag and target_tag not in parent_tags:
        return []
    if not _matches_category_filter(tx_cat, target_cat, include_subclasses, parent_map):
        return []
    projected_amt = (
        _nzd_to_tx_currency(net_nzd, tx)
        if (is_lent and tx.loan_payment_amount is not None)
        else None
    )
    return [ProjectedCashflowItem(transaction=tx, projected_amount=projected_amt)]


def _classify_outflow_splits(
    tx: Transaction,
    tx_cat: str,
    parent_tags: tuple[str, ...],
    meta: _AnnualCategoryMetadata,
    *,
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
) -> tuple[
    list[tuple[TransactionSplit, str, tuple[str, ...], float]],
    list[tuple[TransactionSplit, str, tuple[str, ...], float]],
]:
    """Filter negative non-Transfer splits and partition into active vs excluded."""
    active: list[tuple[TransactionSplit, str, tuple[str, ...], float]] = []
    excluded: list[tuple[TransactionSplit, str, tuple[str, ...], float]] = []
    is_rec_excluded = is_recurrence_excluded_from_period(
        meta.resolved.effective_period, tx.recurrence
    )
    for s in tx.splits:
        signed_amt = s.signed_amount()
        if signed_amt >= 0:
            continue
        s_cat = (s.category or tx_cat).strip() or "General"
        if s_cat.lower() in TRANSFER_CATEGORIES:
            continue
        s_tags = tuple(normalize_tags([*parent_tags, *parse_rule_tags(s.tags)]))
        if target_tag and target_tag not in s_tags:
            continue
        if not _matches_category_filter(s_cat, target_cat, include_subclasses, meta.parent_map):
            continue
        is_excluded = (
            _is_lent_loan(getattr(s, "loan", None))
            or is_rec_excluded
            or meta.is_smoothed_annual(s_cat.lower(), meta.resolve_category(s_cat).lower())
        )
        (excluded if is_excluded else active).append((s, s_cat, s_tags, signed_amt))
    return active, excluded


def _project_outflow_splits(
    tx: Transaction,
    tx_cat: str,
    parent_tags: tuple[str, ...],
    meta: _AnnualCategoryMetadata,
    *,
    category: Optional[str],
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
    include_amortized: bool,
) -> list[ProjectedCashflowItem]:
    """Project negative non-Transfer Manual Split lines partitioned by active vs excluded."""
    active, excluded = _classify_outflow_splits(
        tx,
        tx_cat,
        parent_tags,
        meta,
        target_cat=target_cat,
        target_tag=target_tag,
        include_subclasses=include_subclasses,
    )
    items: list[ProjectedCashflowItem] = []
    if active:
        items.append(
            _build_projected_split_item(
                tx, tx_cat, parent_tags, active, category=category,
                target_cat=target_cat, include_subclasses=include_subclasses, excluded=False,
            )
        )
    if include_amortized and excluded:
        items.append(
            _build_projected_split_item(
                tx, tx_cat, parent_tags, excluded, category=category,
                target_cat=target_cat, include_subclasses=include_subclasses, excluded=True,
            )
        )
    return items


def _build_receipt_outflow_item(
    tx: Transaction,
    tx_cat: str,
    parent_tags: tuple[str, ...],
    tx_nzd: float,
    positive_count: int,
    matched_lines: list[ExpenseLine],
    *,
    category: Optional[str],
    target_cat: Optional[str],
    include_subclasses: bool,
    excluded: bool,
) -> ProjectedCashflowItem:
    """Build a ProjectedCashflowItem from matching scaled Receipt Lines."""
    matched_nzd = round(sum(line.amount_nzd for line in matched_lines), 2)
    matched_cat = _resolve_projected_category(
        tx_cat, {line.category for line in matched_lines}, category, target_cat, include_subclasses
    )
    merged_tags = tuple(normalize_tags(t for line in matched_lines for t in line.effective_tags))
    is_partial = len(matched_lines) < positive_count or abs(matched_nzd - tx_nzd) > 0.01
    if is_partial or (target_cat and matched_cat.lower() != tx_cat.lower()):
        names = ", ".join(line.item_name for line in matched_lines if line.item_name)
        notes = f"Receipt split ({names}) of {tx.currency} {abs(float(tx.amount)):.2f}" if names else tx.notes
        return ProjectedCashflowItem(
            transaction=tx,
            projected_amount=-abs(_nzd_to_tx_currency(matched_nzd, tx)),
            projected_category=matched_cat,
            projected_tags=merged_tags,
            projected_notes=notes,
            is_receipt_split=True,
            excluded_from_period_budget=excluded,
        )
    return ProjectedCashflowItem(
        transaction=tx,
        projected_tags=merged_tags or parent_tags,
        excluded_from_period_budget=excluded,
    )


def _project_lent_unsplit_outflow(
    tx: Transaction,
    tx_cat: str,
    parent_tags: tuple[str, ...],
    parent_map: dict[str, str],
    *,
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
    include_amortized: bool,
) -> list[ProjectedCashflowItem]:
    """Project a lent loan outflow transaction as excluded when include_amortized is enabled."""
    if not include_amortized:
        return []
    if target_tag and target_tag not in parent_tags:
        return []
    if not _matches_category_filter(tx_cat, target_cat, include_subclasses, parent_map):
        return []
    return [ProjectedCashflowItem(transaction=tx, excluded_from_period_budget=True)]


def _project_parent_outflow(
    tx: Transaction,
    tx_cat: str,
    parent_tags: tuple[str, ...],
    meta: _AnnualCategoryMetadata,
    *,
    is_rec_excluded: bool,
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
    include_amortized: bool,
) -> list[ProjectedCashflowItem]:
    """Project an unsplit non-receipt outflow transaction."""
    if target_tag and target_tag not in parent_tags:
        return []
    if not _matches_category_filter(tx_cat, target_cat, include_subclasses, meta.parent_map):
        return []
    is_excluded = is_rec_excluded or meta.is_smoothed_annual(
        tx_cat.lower(), meta.resolve_category(tx_cat).lower()
    )
    if is_excluded and not include_amortized:
        return []
    return [ProjectedCashflowItem(transaction=tx, excluded_from_period_budget=is_excluded)]


def _project_outflow_receipt_or_parent(
    tx: Transaction,
    tx_cat: str,
    parent_tags: tuple[str, ...],
    meta: _AnnualCategoryMetadata,
    *,
    category: Optional[str],
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
    include_amortized: bool,
) -> list[ProjectedCashflowItem]:
    """Project an unsplit outflow via Receipt Lines (excluding Transfers) or parent tx."""
    if float(tx.amount) >= 0 or tx_cat.lower() in TRANSFER_CATEGORIES:
        return []
    if _is_lent_loan(getattr(tx, "loan", None)):
        return _project_lent_unsplit_outflow(
            tx, tx_cat, parent_tags, meta.parent_map,
            target_cat=target_cat, target_tag=target_tag,
            include_subclasses=include_subclasses, include_amortized=include_amortized,
        )

    scaled_lines, positive_count, tx_nzd = _scale_receipt_expense_lines(tx, tx_cat, list(parent_tags))
    is_rec_excluded = is_recurrence_excluded_from_period(meta.resolved.effective_period, tx.recurrence)
    if positive_count > 0:
        return _partition_receipt_outflow_lines(
            tx, tx_cat, parent_tags, meta, tx_nzd, positive_count, scaled_lines,
            is_rec_excluded=is_rec_excluded, category=category, target_cat=target_cat,
            target_tag=target_tag, include_subclasses=include_subclasses,
            include_amortized=include_amortized,
        )
    return _project_parent_outflow(
        tx, tx_cat, parent_tags, meta, is_rec_excluded=is_rec_excluded,
        target_cat=target_cat, target_tag=target_tag,
        include_subclasses=include_subclasses, include_amortized=include_amortized,
    )


def _partition_receipt_outflow_lines(
    tx: Transaction,
    tx_cat: str,
    parent_tags: tuple[str, ...],
    meta: _AnnualCategoryMetadata,
    tx_nzd: float,
    positive_count: int,
    scaled_lines: list[ExpenseLine],
    *,
    is_rec_excluded: bool,
    category: Optional[str],
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
    include_amortized: bool,
) -> list[ProjectedCashflowItem]:
    """Filter scaled Receipt Lines and emit active and optional excluded slices."""
    active: list[ExpenseLine] = []
    excluded: list[ExpenseLine] = []
    for line in scaled_lines:
        if target_tag and target_tag not in line.effective_tags:
            continue
        if not _matches_category_filter(line.category, target_cat, include_subclasses, meta.parent_map):
            continue
        is_excluded = is_rec_excluded or meta.is_smoothed_annual(
            line.category.lower(), meta.resolve_category(line.category).lower()
        )
        (excluded if is_excluded else active).append(line)

    slices = [(active, False)] + ([(excluded, True)] if include_amortized and excluded else [])
    return [
        _build_receipt_outflow_item(
            tx, tx_cat, parent_tags, tx_nzd, positive_count, grp,
            category=category, target_cat=target_cat,
            include_subclasses=include_subclasses, excluded=is_excl,
        )
        for grp, is_excl in slices
        if grp
    ]


def _extract_split_tags_for_category(tx: Transaction, target_category: str) -> tuple[str, ...]:
    """Combine parent transaction tags with negative split tags matching target_category."""
    tx_cat = (tx.category or "General").strip() or "General"
    cat_lower = target_category.strip().lower()
    tags = list(parse_rule_tags(tx.tags))
    for s in getattr(tx, "splits", None) or ():
        s_cat = (s.category or tx_cat).strip().lower()
        if s.signed_amount() < 0 and s_cat == cat_lower:
            tags.extend(parse_rule_tags(s.tags))
    return tuple(normalize_tags(tags))


def _build_amortized_cashflow_item(
    rec_tx: Transaction,
    alloc: RecurringCommitmentAllocation,
    is_split_slice: bool,
    meta: _AnnualCategoryMetadata,
    *,
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
    account_id: Optional[int],
    loan_id: Optional[int],
) -> Optional[ProjectedCashflowItem]:
    """Convert an active RecurringCommitmentAllocation into ProjectedCashflowItem if filters match."""
    if alloc.recurrence not in meta.resolved.amortized_recurrences:
        return None
    if not _matches_account_filter(rec_tx, account_id) or not _matches_loan_filter(rec_tx, loan_id):
        return None
    if meta.is_smoothed_annual(alloc.category.lower(), meta.resolve_category(alloc.category).lower()):
        return None
    if not _matches_category_filter(alloc.category, target_cat, include_subclasses, meta.parent_map):
        return None
    effective_tags = (
        _extract_split_tags_for_category(rec_tx, alloc.category)
        if is_split_slice
        else tuple(parse_rule_tags(rec_tx.tags))
    )
    if target_tag and target_tag not in effective_tags:
        return None
    cadence = "month" if meta.resolved.is_monthly_cadence else "week"
    amortized_nzd = alloc.monthly_nzd if meta.resolved.is_monthly_cadence else alloc.weekly_nzd
    return ProjectedCashflowItem(
        transaction=rec_tx,
        projected_amount=-abs(alloc.original_amount) if is_split_slice else None,
        projected_category=alloc.category if is_split_slice else None,
        projected_tags=effective_tags if is_split_slice else None,
        is_split=True if is_split_slice else None,
        is_amortized_commitment=True,
        amortized_period_amount_nzd=amortized_nzd,
        amortized_cadence=cadence,
    )


def _collect_cashflow_amortized_commitments(
    db: Session,
    meta: _AnnualCategoryMetadata,
    *,
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
    account_id: Optional[int],
    loan_id: Optional[int],
) -> list[ProjectedCashflowItem]:
    """Collect active amortized Recurring Commitments aligned with project_window."""
    if not meta.resolved.amortized_recurrences:
        return []
    items: list[ProjectedCashflowItem] = []
    for rec_tx, alloc, is_split_slice in _iter_active_recurring_allocations(
        db, meta.resolved.as_of_date, eager_hydrate=True
    ):
        built = _build_amortized_cashflow_item(
            rec_tx, alloc, is_split_slice, meta,
            target_cat=target_cat, target_tag=target_tag,
            include_subclasses=include_subclasses, account_id=account_id, loan_id=loan_id,
        )
        if built is not None:
            items.append(built)
    items.sort(
        key=lambda item: (
            -float(item.amortized_period_amount_nzd or 0.0),
            -item.transaction.transaction_date.toordinal(),
            -item.transaction.id,
            (item.projected_category or item.transaction.category or "").lower(),
        )
    )
    return items


def _project_cashflow_row(
    tx: Transaction,
    meta: _AnnualCategoryMetadata,
    *,
    flow: FlowLiteral,
    category: Optional[str],
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
    include_amortized: bool,
) -> list[ProjectedCashflowItem]:
    """Project one Transaction into zero, one, or partitioned cashflow items."""
    tx_cat = (tx.category or "General").strip() or "General"
    parent_tags = tuple(parse_rule_tags(tx.tags))
    has_splits = bool(getattr(tx, "splits", None))
    if flow == "inflow":
        if has_splits:
            return _project_inflow_splits(
                tx, tx_cat, parent_tags, category=category, target_cat=target_cat,
                target_tag=target_tag, include_subclasses=include_subclasses,
                parent_map=meta.parent_map,
            )
        return _project_unsplit_inflow(
            tx, tx_cat, parent_tags, target_cat=target_cat, target_tag=target_tag,
            include_subclasses=include_subclasses, parent_map=meta.parent_map,
        )
    if has_splits:
        return _project_outflow_splits(
            tx, tx_cat, parent_tags, meta, category=category, target_cat=target_cat,
            target_tag=target_tag, include_subclasses=include_subclasses,
            include_amortized=include_amortized,
        )
    return _project_outflow_receipt_or_parent(
        tx, tx_cat, parent_tags, meta, category=category, target_cat=target_cat,
        target_tag=target_tag, include_subclasses=include_subclasses,
        include_amortized=include_amortized,
    )


def _paginate_and_attach_commitments(
    db: Session,
    meta: _AnnualCategoryMetadata,
    regular_rows: list[ProjectedCashflowItem],
    *,
    flow: FlowLiteral,
    effective_amortized: bool,
    offset: int,
    limit: int,
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
    account_id: Optional[int],
    loan_id: Optional[int],
) -> CashflowProjectionResult:
    """Sort regular cashflow items newest-first, slice page, and attach commitments on offset=0."""
    regular_rows.sort(
        key=lambda item: (
            -item.transaction.transaction_date.toordinal(),
            -item.transaction.id,
            bool(item.excluded_from_period_budget),
        )
    )
    total_count = len(regular_rows)
    sliced = list(regular_rows[offset : offset + limit])
    if flow == "outflow" and effective_amortized and offset == 0:
        sliced.extend(
            _collect_cashflow_amortized_commitments(
                db, meta, target_cat=target_cat, target_tag=target_tag,
                include_subclasses=include_subclasses, account_id=account_id, loan_id=loan_id,
            )
        )
    return CashflowProjectionResult(items=tuple(sliced), total_count=total_count)


def project_cashflow_transactions(
    db: Session,
    window: ReportingWindow,
    *,
    flow: FlowLiteral,
    category: Optional[str] = None,
    include_subclasses: bool = False,
    include_amortized: bool = False,
    account_id: Optional[int] = None,
    loan_id: Optional[int] = None,
    tag: Optional[str] = None,
    offset: int = 0,
    limit: int = 100,
    reference_date: date | None = None,
) -> CashflowProjectionResult:
    """Project paginated inflow or outflow transactions with exact project_window parity."""
    if flow not in ("inflow", "outflow"):
        raise InvalidWindowError("flow must be 'inflow' or 'outflow'")
    if offset < 0:
        raise InvalidWindowError("offset must be non-negative")
    if limit < 1:
        raise InvalidWindowError("limit must be positive")

    resolved = resolve_reporting_window(window, reference_date=reference_date)
    meta = _load_annual_metadata(db, resolved, rollup_base=include_subclasses)
    target_cat = category.strip().lower() if category and category.strip() else None
    target_tag = tag.strip().lower().lstrip("#").replace(" ", "-") if tag else None
    effective_amortized = include_amortized if flow == "outflow" else False

    rows = _query_window_transactions(
        db, resolved, account_id=account_id, loan_id=loan_id, eager_hydrate=True
    )
    regular_rows: list[ProjectedCashflowItem] = []
    for tx in rows:
        regular_rows.extend(
            _project_cashflow_row(
                tx, meta, flow=flow, category=category, target_cat=target_cat,
                target_tag=target_tag, include_subclasses=include_subclasses,
                include_amortized=effective_amortized,
            )
        )
    return _paginate_and_attach_commitments(
        db, meta, regular_rows, flow=flow, effective_amortized=effective_amortized,
        offset=offset, limit=limit, target_cat=target_cat, target_tag=target_tag,
        include_subclasses=include_subclasses, account_id=account_id, loan_id=loan_id,
    )
