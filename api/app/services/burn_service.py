"""Single-responsibility weekly burn, FX repair, and average-burn budget service."""

# ============================================================================
# 1. IMPORTS & CONSTANTS
# ============================================================================
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.models import Account, AssetSnapshot, BudgetRule, Category, Receipt, Transaction, TransactionSplit
from app.services.category_taxonomy import get_category_parent_map, get_parent_category
from app.services.fx_service import FxRateFetcher, get_exchange_rate, to_nzd_amount
from app.services.recurrence_service import get_active_recurring_commitments
from app.services.rules_engine import normalize_tags, parse_rule_tags

TRANSFER_CATEGORIES = ("transfers", "transfer")
DEFAULT_WEEKLY_BURN = 1000.0
WEEKS_PER_YEAR = 52.0
MONTHS_PER_YEAR = 12.0
DAYS_PER_YEAR = 365
MIN_SIGNIFICANT_WEEKLY_BURN = 1.0
DEFAULT_CATEGORIES = [
    "Groceries",
    "Dining",
    "Alcohol",
    "Fuel",
    "Transit",
    "Bills & Telco",
    "Software & AI",
    "Housing",
    "General",
]


# ============================================================================
# 2. DOMAIN MODELS & PURE HELPERS (Zero DB I/O)
# ============================================================================
@dataclass(frozen=True)
class WeeklyBurnSummary:
    """Decomposed weekly burn telemetry and current-week spending snapshot."""

    effective_weekly_burn: float
    variable_weekly_burn: float
    recurring_weekly_burn: float
    this_week_spend: float
    this_week_variable_spend: float
    weekly_budget_target: float
    lookback_weeks: int


@dataclass(frozen=True)
class CategoryBurnEstimate:
    """Average weekly burn breakdown and budget limit suggestions for one category."""

    category: str
    variable_weekly_burn: float
    recurring_weekly_burn: float
    average_weekly_burn: float
    suggested_monthly_limit: float
    suggested_annual_limit: float = 0.0
    budget_period: str = "monthly"
    variable_percentage: float = 0.0
    total_percentage: float = 0.0
    parent_category: Optional[str] = None


@dataclass(frozen=True)
class RecurringCommitmentItem:
    """Itemized active recurring commitment normalized to weekly and monthly NZD cost."""

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


def weekly_to_monthly(weekly_amount: float) -> float:
    """Convert a weekly NZD limit to its 52/12 monthly equivalent."""
    return round(float(weekly_amount) * WEEKS_PER_YEAR / MONTHS_PER_YEAR, 2)


def monthly_to_weekly(monthly_amount: float) -> float:
    """Convert a monthly NZD limit to its 12/52 weekly equivalent."""
    return round(float(monthly_amount) * MONTHS_PER_YEAR / WEEKS_PER_YEAR, 2)


def weekly_to_annual(weekly_amount: float) -> float:
    """Convert a weekly NZD limit to its 52-week annual equivalent."""
    return round(float(weekly_amount) * WEEKS_PER_YEAR, 2)


def annual_to_weekly(annual_amount: float) -> float:
    """Convert an annual NZD limit to its 52-week amortized weekly equivalent."""
    return round(float(annual_amount) / WEEKS_PER_YEAR, 2)


# ============================================================================
# 3. ACCOUNT & CATEGORY REPOSITORY HELPERS
# ============================================================================
def get_latest_account_balances(db: Session) -> dict[str, float]:
    """Retrieve the latest balance in NZD for all active accounts grouped by tier."""
    accounts = db.scalars(select(Account).where(Account.is_active == True)).all()
    balances = {"cash": 0.0, "growth": 0.0, "locked": 0.0}

    for acc in accounts:
        latest_snapshot = db.scalar(
            select(AssetSnapshot)
            .where(AssetSnapshot.account_id == acc.id)
            .order_by(desc(AssetSnapshot.snapshot_date))
        )
        bal_nzd = float(latest_snapshot.balance_nzd) if latest_snapshot else 0.0
        tier = acc.tier.lower()
        balances[tier if tier in balances else "cash"] += bal_nzd

    return balances


def resolve_tier_labels(db: Session) -> dict[str, str]:
    """Resolve display labels for Cash, Growth, and Locked tiers from active accounts."""
    active_accounts = db.scalars(select(Account).where(Account.is_active == True)).all()
    locked_accs = [a.name for a in active_accounts if a.tier.lower() == "locked"]
    growth_accs = [a.name for a in active_accounts if a.tier.lower() == "growth"]
    cash_accs = [
        a.name
        for a in active_accounts
        if a.tier.lower() == "cash" and a.account_type != "credit_card"
    ]
    return {
        "cash": cash_accs[0] if len(cash_accs) == 1 else "Cash Floor",
        "growth": growth_accs[0] if len(growth_accs) == 1 else "Growth",
        "locked": locked_accs[0] if len(locked_accs) == 1 else "Locked Reserve",
    }


def get_active_categories(db: Session) -> list[str]:
    """Return sorted active expense categories from Category table, BudgetRules, ledger, or defaults."""
    category_rows = list(
        db.scalars(
            select(Category.name)
            .where(Category.is_active == True)
            .order_by(Category.name)
        ).all()
    )
    if category_rows:
        filtered = [
            name
            for name in category_rows
            if name and name.strip().lower() not in (*TRANSFER_CATEGORIES, "income", "general")
        ]
        if filtered:
            return filtered

    active_categories = list(
        db.scalars(
            select(BudgetRule.category)
            .where(BudgetRule.is_active == True)
            .order_by(BudgetRule.category)
        ).all()
    )
    if active_categories:
        return active_categories

    tx_cats = db.scalars(select(Transaction.category).distinct()).all()
    discovered = sorted({c for c in tx_cats if c})
    return discovered or list(DEFAULT_CATEGORIES)


# ============================================================================
# 4. FX INTEGRITY REPAIR
# ============================================================================
def repair_transaction_fx_rates(
    db: Session,
    *,
    fetcher: Optional[FxRateFetcher] = None,
) -> int:
    """Repair corrupted NZD exchange rates and backfill missing foreign-currency FX rates."""
    candidates = db.scalars(
        select(Transaction).where(
            Transaction.linked_transaction_id.is_(None),
            or_(
                (func.upper(Transaction.currency) == "NZD")
                & Transaction.exchange_rate.is_not(None)
                & (Transaction.exchange_rate != 1.0),
                (func.upper(Transaction.currency) != "NZD")
                & (
                    Transaction.exchange_rate.is_(None)
                    | (Transaction.exchange_rate <= 0)
                    | (
                        (func.upper(Transaction.currency) == "SGD")
                        & (Transaction.exchange_rate < 1.20)
                    )
                ),
            ),
        )
    ).all()

    repaired = 0
    for tx in candidates:
        code = (tx.currency or "NZD").strip().upper()
        if code == "NZD":
            tx.exchange_rate = 1.0
            repaired += 1
            continue

        resolved = get_exchange_rate(
            db,
            rate_date=tx.transaction_date,
            from_currency=code,
            to_currency="NZD",
            fetcher=fetcher,
        )
        if resolved is not None and resolved[0] > 0:
            tx.exchange_rate = round(float(resolved[0]), 8)
            repaired += 1

    if repaired > 0:
        db.commit()
    return repaired


# ============================================================================
# 5. WEEKLY BURN & AVERAGE-BURN BUDGET ENGINE
# ============================================================================
def get_active_budget_periods(db: Session) -> dict[str, str]:
    """Return lowercase category -> configured period ('weekly', 'monthly', 'annual').

    Merges active BudgetRule periods with category-level 52-week annual
    amortization settings so any category marked `amortize_period='annual'`
    spreads over 52 weeks even without an explicit BudgetRule.
    """
    periods: dict[str, str] = {}
    rows = db.execute(
        select(BudgetRule.category, BudgetRule.budget_period).where(BudgetRule.is_active == True)
    ).all()
    for cat, period in rows:
        if cat and cat.strip():
            periods[cat.strip().lower()] = (period or "monthly").strip().lower()

    cat_rows = db.execute(
        select(Category.name, Category.amortize_period).where(
            Category.is_active == True,
            Category.amortize_period.is_not(None),
        )
    ).all()
    for cat_name, amortize_period in cat_rows:
        if not cat_name or not cat_name.strip():
            continue
        if (amortize_period or "").strip().lower() == "annual":
            periods[cat_name.strip().lower()] = "annual"

    return periods


def get_annual_budget_categories(db: Session) -> set[str]:
    """Return lowercase category keys configured as 52-week annual budgets."""
    return {cat for cat, period in get_active_budget_periods(db).items() if period == "annual"}


@dataclass(frozen=True)
class ExpandedReceiptLine:
    """Single expense allocation from a transaction or matched receipt item."""

    category: str
    amount_nzd: float
    effective_tags: list[str]
    item_name: Optional[str] = None


def expand_expense_lines_by_receipt(tx: Transaction) -> list[ExpandedReceiptLine]:
    """Expand an expense transaction into manual split lines or itemized receipt allocations in NZD.

    Precedence: manual `tx.splits` -> `tx.receipt.items` -> parent `tx`.
    Manual split lines compute each line in NZD directly and skip `lent` loan
    disbursements and `Transfers` lines without inflating remaining lines.
    """
    tx_cat = (tx.category or "General").strip() or "General"
    parent_tags = parse_rule_tags(tx.tags)
    splits = getattr(tx, "splits", None) or []
    if splits:
        split_result: list[ExpandedReceiptLine] = []
        for s in splits:
            raw_cat = (s.category or tx_cat).strip() or tx_cat
            if raw_cat.lower() in TRANSFER_CATEGORIES:
                continue
            if s.loan is not None and (s.loan.loan_type or "").strip().lower() == "lent":
                continue
            line_nzd = round(
                to_nzd_amount(abs(float(s.amount)), tx.currency, tx.exchange_rate),
                2,
            )
            line_tags = normalize_tags([*parent_tags, *parse_rule_tags(s.tags)])
            split_result.append(
                ExpandedReceiptLine(
                    category=raw_cat,
                    amount_nzd=line_nzd,
                    effective_tags=line_tags,
                    item_name=s.notes,
                )
            )
        return split_result

    tx_nzd = round(to_nzd_amount(abs(float(tx.amount)), tx.currency, tx.exchange_rate), 2)
    receipt = getattr(tx, "receipt", None)
    items = getattr(receipt, "items", None) if receipt is not None else None
    positive_items = [it for it in (items or []) if float(it.total_price) > 0]
    items_sum = sum(float(it.total_price) for it in positive_items)
    if items_sum <= 0 or not positive_items:
        return [ExpandedReceiptLine(category=tx_cat, amount_nzd=tx_nzd, effective_tags=parent_tags)]

    scale = tx_nzd / items_sum
    result: list[ExpandedReceiptLine] = []
    for it in positive_items:
        raw_cat = (it.category or tx_cat).strip() or tx_cat
        item_tags = normalize_tags([*parent_tags, *parse_rule_tags(it.tags)])
        result.append(
            ExpandedReceiptLine(
                category=raw_cat,
                amount_nzd=round(float(it.total_price) * scale, 2),
                effective_tags=item_tags,
                item_name=it.item_name,
            )
        )
    delta = round(tx_nzd - sum(line.amount_nzd for line in result), 2)
    if delta != 0.0 and result:
        max_idx = max(range(len(result)), key=lambda i: result[i].amount_nzd)
        target = result[max_idx]
        result[max_idx] = ExpandedReceiptLine(
            category=target.category,
            amount_nzd=round(target.amount_nzd + delta, 2),
            effective_tags=target.effective_tags,
            item_name=target.item_name,
        )
    return result


def expand_expense_transaction_by_receipt(tx: Transaction) -> list[tuple[str, float]]:
    """Split a matched expense transaction into itemized receipt category buckets in NZD.

    If no linked receipt items exist, returns the parent transaction's single
    (category, nzd_amount) tuple. Totals are scaled and rounded so sum(buckets) == tx_nzd.
    """
    grouped: dict[str, float] = {}
    canonical: dict[str, str] = {}
    for line in expand_expense_lines_by_receipt(tx):
        key = line.category.lower()
        canonical.setdefault(key, line.category)
        grouped[key] = grouped.get(key, 0.0) + line.amount_nzd
    return [(canonical[key], round(amt, 2)) for key, amt in grouped.items()]


def _fetch_expanded_variable_expenses(
    db: Session,
    start_date: date,
    end_date: date,
    *,
    include_categories: set[str] | None = None,
    exclude_categories: set[str] | tuple[str, ...] = (),
) -> list[tuple[str, float]]:
    """Query non-recurring, non-transfer expenses and expand matched receipts by item category."""
    excluded_tx = tuple({c.lower() for c in TRANSFER_CATEGORIES})
    txs = db.scalars(
        select(Transaction)
        .options(
            joinedload(Transaction.receipt).joinedload(Receipt.items),
            joinedload(Transaction.loan),
            joinedload(Transaction.splits).joinedload(TransactionSplit.loan),
        )
        .where(
            Transaction.transaction_date >= start_date,
            Transaction.transaction_date <= end_date,
            Transaction.amount < 0,
            Transaction.recurrence.is_(None),
            or_(
                func.lower(Transaction.category).not_in(excluded_tx),
                Transaction.splits.any(),
            ),
        )
    ).unique().all()

    inc_lower = {c.strip().lower() for c in include_categories if c and c.strip()} if include_categories is not None else None
    exc_lower = {c.lower() for c in TRANSFER_CATEGORIES} | {
        c.strip().lower() for c in exclude_categories if c and c.strip()
    }

    expanded: list[tuple[str, float]] = []
    for tx in txs:
        if (
            not getattr(tx, "splits", None)
            and tx.loan is not None
            and (tx.loan.loan_type or "").strip().lower() == "lent"
        ):
            continue
        for cat_name, amt_nzd in expand_expense_transaction_by_receipt(tx):
            key = cat_name.lower()
            if key in exc_lower:
                continue
            if inc_lower is not None and key not in inc_lower:
                continue
            expanded.append((cat_name, amt_nzd))
    return expanded


def calculate_annual_category_spend_map(
    db: Session,
    as_of: date,
    categories: set[str],
) -> dict[str, float]:
    """Sum trailing 365-day NZD spend per lowercase category for annual-budget categories."""
    if not categories:
        return {}
    rows = _fetch_expanded_variable_expenses(
        db,
        as_of - timedelta(days=DAYS_PER_YEAR),
        as_of,
        include_categories=categories,
    )
    totals: dict[str, float] = {}
    for cat_name, amt_nzd in rows:
        key = cat_name.lower()
        totals[key] = totals.get(key, 0.0) + amt_nzd
    return totals


def calculate_annual_category_count_map(
    db: Session,
    as_of: date,
    categories: set[str],
) -> dict[str, int]:
    """Count trailing 365-day non-recurring expense rows per lowercase category for annual budgets."""
    if not categories:
        return {}
    rows = _fetch_expanded_variable_expenses(
        db,
        as_of - timedelta(days=DAYS_PER_YEAR),
        as_of,
        include_categories=categories,
    )
    counts: dict[str, int] = {}
    for cat_name, _ in rows:
        key = cat_name.lower()
        counts[key] = counts.get(key, 0) + 1
    return counts


def _sum_variable_spend_nzd(
    db: Session,
    start_date: date,
    end_date: date,
    *,
    exclude_categories: set[str] | tuple[str, ...] = (),
) -> float:
    """Sum NZD-normalized variable expenses in [start_date, end_date]."""
    rows = _fetch_expanded_variable_expenses(
        db, start_date, end_date, exclude_categories=exclude_categories
    )
    return sum(amt_nzd for _, amt_nzd in rows)


def _get_active_weekly_budget(db: Session) -> float:
    """Return total active weekly budget limit across all configured rules."""
    budget_sum = db.scalar(
        select(func.sum(BudgetRule.weekly_limit)).where(BudgetRule.is_active == True)
    )
    return float(budget_sum or 0.0)


def calculate_weekly_burn_summary(
    db: Session,
    as_of: date,
    lookback_weeks: int = 4,
) -> WeeklyBurnSummary:
    """Calculate effective weekly burn (variable average + amortized recurring) and this-week spend."""
    weekly_budget = _get_active_weekly_budget(db)
    annual_cats = get_annual_budget_categories(db)
    lookback_start = as_of - timedelta(days=lookback_weeks * 7)

    regular_total_nzd = _sum_variable_spend_nzd(
        db, lookback_start, as_of, exclude_categories=annual_cats
    )
    regular_weekly_burn = regular_total_nzd / float(lookback_weeks)

    annual_spend_map = calculate_annual_category_spend_map(db, as_of, annual_cats)
    annual_weekly_burn = sum(annual_spend_map.values()) / WEEKS_PER_YEAR

    variable_weekly_burn = round(regular_weekly_burn + annual_weekly_burn, 2)

    recurring_weekly_burn = round(
        sum(weekly_nzd for _, weekly_nzd in get_active_recurring_commitments(db, as_of)),
        2,
    )
    trailing_weekly_burn = round(variable_weekly_burn + recurring_weekly_burn, 2)

    start_of_week = as_of - timedelta(days=as_of.weekday())
    this_week_regular = _sum_variable_spend_nzd(
        db, start_of_week, as_of, exclude_categories=annual_cats
    )
    this_week_variable_spend = round(this_week_regular + annual_weekly_burn, 2)
    this_week_spend = round(this_week_variable_spend + recurring_weekly_burn, 2)

    if trailing_weekly_burn > 0.0:
        effective_burn = trailing_weekly_burn
    elif weekly_budget > 0.0:
        effective_burn = round(weekly_budget, 2)
    else:
        effective_burn = DEFAULT_WEEKLY_BURN

    target_budget = round(weekly_budget, 2) if weekly_budget > 0.0 else effective_burn

    return WeeklyBurnSummary(
        effective_weekly_burn=effective_burn,
        variable_weekly_burn=variable_weekly_burn,
        recurring_weekly_burn=recurring_weekly_burn,
        this_week_spend=this_week_spend,
        this_week_variable_spend=this_week_variable_spend,
        weekly_budget_target=target_budget,
        lookback_weeks=lookback_weeks,
    )


def get_effective_weekly_burn(db: Session, as_of: date) -> tuple[float, float, float]:
    """Return (effective_weekly_burn, this_week_spend, weekly_budget_target)."""
    summary = calculate_weekly_burn_summary(db, as_of)
    return (
        summary.effective_weekly_burn,
        summary.this_week_spend,
        summary.weekly_budget_target,
    )


def calculate_category_average_burn(
    db: Session,
    as_of: date,
    lookback_weeks: int = 12,
) -> list[CategoryBurnEstimate]:
    """Calculate per-category weekly burn averages, spreading annual categories over 52 weeks."""
    rule_periods = get_active_budget_periods(db)
    annual_cats = {k for k, period in rule_periods.items() if period == "annual"}

    lookback_start = as_of - timedelta(days=lookback_weeks * 7)
    variable_rows = _fetch_expanded_variable_expenses(
        db, lookback_start, as_of, exclude_categories=annual_cats
    )

    canonical_names: dict[str, str] = {}
    variable_totals: dict[str, float] = {}
    for cat_name, amt_nzd in variable_rows:
        key = cat_name.lower()
        canonical_names.setdefault(key, cat_name)
        variable_totals[key] = variable_totals.get(key, 0.0) + amt_nzd

    annual_rows = _fetch_expanded_variable_expenses(
        db, as_of - timedelta(days=DAYS_PER_YEAR), as_of, include_categories=annual_cats
    )
    annual_totals: dict[str, float] = {}
    for cat_name, amt_nzd in annual_rows:
        key = cat_name.lower()
        canonical_names.setdefault(key, cat_name)
        annual_totals[key] = annual_totals.get(key, 0.0) + amt_nzd

    recurring_totals: dict[str, float] = {}
    for rec_tx, weekly_nzd in get_active_recurring_commitments(db, as_of):
        cat_name = (rec_tx.category or "General").strip() or "General"
        key = cat_name.lower()
        canonical_names.setdefault(key, cat_name)
        recurring_totals[key] = recurring_totals.get(key, 0.0) + weekly_nzd

    raw_rows: list[tuple[str, str, float, float, float]] = []
    for key, display_name in canonical_names.items():
        if key in annual_cats:
            var_wk = round(annual_totals.get(key, 0.0) / WEEKS_PER_YEAR, 2)
        else:
            var_wk = round(variable_totals.get(key, 0.0) / float(lookback_weeks), 2)
        rec_wk = round(recurring_totals.get(key, 0.0), 2)
        avg_wk = round(var_wk + rec_wk, 2)
        raw_rows.append((key, display_name, var_wk, rec_wk, avg_wk))

    total_var_wk = sum(item[2] for item in raw_rows)
    total_avg_wk = sum(item[4] for item in raw_rows)
    parent_map = get_category_parent_map(db)

    estimates = [
        CategoryBurnEstimate(
            category=display_name,
            parent_category=get_parent_category(display_name, parent_map),
            variable_weekly_burn=var_wk,
            recurring_weekly_burn=rec_wk,
            average_weekly_burn=avg_wk,
            suggested_monthly_limit=weekly_to_monthly(avg_wk),
            suggested_annual_limit=weekly_to_annual(avg_wk),
            budget_period=rule_periods.get(key, "monthly"),
            variable_percentage=round(var_wk / total_var_wk * 100.0, 1) if total_var_wk > 0 else 0.0,
            total_percentage=round(avg_wk / total_avg_wk * 100.0, 1) if total_avg_wk > 0 else 0.0,
        )
        for key, display_name, var_wk, rec_wk, avg_wk in raw_rows
    ]

    estimates.sort(key=lambda item: (-item.average_weekly_burn, item.category))
    return estimates


def list_recurring_commitment_details(
    db: Session,
    as_of: date,
) -> list[RecurringCommitmentItem]:
    """Return itemized active fixed recurring commitments with NZD weekly/monthly shares."""
    raw_commitments = get_active_recurring_commitments(db, as_of)
    total_rec_wk = sum(weekly_nzd for _, weekly_nzd in raw_commitments)

    items = [
        RecurringCommitmentItem(
            transaction_id=tx.id,
            merchant=(tx.merchant or "").strip() or "Unnamed commitment",
            category=(tx.category or "General").strip() or "General",
            recurrence=(tx.recurrence or "").strip().lower(),
            last_charged_date=tx.transaction_date,
            original_amount=round(abs(float(tx.amount)), 2),
            currency=(tx.currency or "NZD").strip().upper(),
            amount_nzd=round(to_nzd_amount(tx.amount, tx.currency, tx.exchange_rate), 2),
            weekly_nzd=round(weekly_nzd, 2),
            monthly_nzd=weekly_to_monthly(weekly_nzd),
            percentage_of_recurring=(
                round(weekly_nzd / total_rec_wk * 100.0, 1) if total_rec_wk > 0 else 0.0
            ),
        )
        for tx, weekly_nzd in raw_commitments
    ]
    items.sort(key=lambda item: (-item.weekly_nzd, item.merchant.lower()))
    return items


def sync_budgets_from_average_burn(
    db: Session,
    as_of: date,
    lookback_weeks: int = 12,
) -> list[BudgetRule]:
    """Upsert active category BudgetRules directly from actual average weekly burn."""
    estimates = calculate_category_average_burn(db, as_of, lookback_weeks=lookback_weeks)
    existing_rules = {
        rule.category.lower(): rule
        for rule in db.scalars(select(BudgetRule)).all()
    }

    for est in estimates:
        if est.average_weekly_burn < MIN_SIGNIFICANT_WEEKLY_BURN:
            continue
        existing = existing_rules.get(est.category.lower())
        if existing:
            existing.weekly_limit = est.average_weekly_burn
            existing.monthly_limit = est.suggested_monthly_limit
            existing.is_active = True
        else:
            db.add(
                BudgetRule(
                    category=est.category,
                    weekly_limit=est.average_weekly_burn,
                    monthly_limit=est.suggested_monthly_limit,
                    budget_period=est.budget_period or "monthly",
                    is_active=True,
                )
            )

    db.commit()
    return list(db.scalars(select(BudgetRule).order_by(BudgetRule.category)).all())
