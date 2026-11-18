"""Single-responsibility weekly burn, FX repair, and average-burn budget service."""

# ============================================================================
# 1. IMPORTS & CONSTANTS
# ============================================================================
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session

from app.models import Account, AssetSnapshot, BudgetRule, Transaction
from app.services.fx_service import FxRateFetcher, get_exchange_rate, to_nzd_amount
from app.services.recurrence_service import get_active_recurring_commitments

TRANSFER_CATEGORIES = ("transfers", "transfer")
DEFAULT_WEEKLY_BURN = 1000.0
WEEKS_PER_YEAR = 52.0
MONTHS_PER_YEAR = 12.0
DAYS_PER_YEAR = 365
MIN_SIGNIFICANT_WEEKLY_BURN = 1.0
DEFAULT_CATEGORIES = [
    "Groceries",
    "Dining",
    "Transport",
    "Subscriptions",
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
    """Return sorted active budget categories, falling back to distinct ledger or defaults."""
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
    """Return lowercase category -> configured budget_period ('weekly', 'monthly', 'annual')."""
    rows = db.execute(
        select(BudgetRule.category, BudgetRule.budget_period).where(BudgetRule.is_active == True)
    ).all()
    return {
        cat.strip().lower(): (period or "monthly").strip().lower()
        for cat, period in rows
        if cat and cat.strip()
    }


def get_annual_budget_categories(db: Session) -> set[str]:
    """Return lowercase category keys configured as 52-week annual budgets."""
    return {cat for cat, period in get_active_budget_periods(db).items() if period == "annual"}


def _fetch_variable_expense_rows(
    db: Session,
    start_date: date,
    end_date: date,
    *,
    exclude_categories: set[str] | tuple[str, ...] = (),
):
    """Query non-recurring, non-transfer expense rows in [start_date, end_date]."""
    excluded = tuple(
        {c.lower() for c in TRANSFER_CATEGORIES}
        | {c.strip().lower() for c in exclude_categories if c and c.strip()}
    )
    return db.execute(
        select(
            Transaction.category,
            Transaction.amount,
            Transaction.currency,
            Transaction.exchange_rate,
        ).where(
            Transaction.transaction_date >= start_date,
            Transaction.transaction_date <= end_date,
            Transaction.amount < 0,
            Transaction.recurrence.is_(None),
            func.lower(Transaction.category).not_in(excluded),
        )
    ).all()


def _fetch_specific_category_expense_rows(
    db: Session,
    start_date: date,
    end_date: date,
    categories: set[str],
):
    """Query non-recurring expense rows matching any category in categories (lowercase)."""
    normalized = tuple(sorted({c.strip().lower() for c in categories if c and c.strip()}))
    if not normalized:
        return []
    return db.execute(
        select(
            Transaction.category,
            Transaction.amount,
            Transaction.currency,
            Transaction.exchange_rate,
        ).where(
            Transaction.transaction_date >= start_date,
            Transaction.transaction_date <= end_date,
            Transaction.amount < 0,
            Transaction.recurrence.is_(None),
            func.lower(Transaction.category).in_(normalized),
        )
    ).all()


def calculate_annual_category_spend_map(
    db: Session,
    as_of: date,
    categories: set[str],
) -> dict[str, float]:
    """Sum trailing 365-day NZD spend per lowercase category for annual-budget categories."""
    if not categories:
        return {}
    rows = _fetch_specific_category_expense_rows(
        db,
        as_of - timedelta(days=DAYS_PER_YEAR),
        as_of,
        categories,
    )
    totals: dict[str, float] = {}
    for raw_cat, amt, cur, fx in rows:
        key = (raw_cat or "General").strip().lower() or "general"
        totals[key] = totals.get(key, 0.0) + to_nzd_amount(amt, cur, fx)
    return totals


def _sum_variable_spend_nzd(
    db: Session,
    start_date: date,
    end_date: date,
    *,
    exclude_categories: set[str] | tuple[str, ...] = (),
) -> float:
    """Sum NZD-normalized variable expenses in [start_date, end_date]."""
    rows = _fetch_variable_expense_rows(
        db, start_date, end_date, exclude_categories=exclude_categories
    )
    return sum(to_nzd_amount(amt, cur, fx) for _, amt, cur, fx in rows)


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
    variable_rows = _fetch_variable_expense_rows(
        db, lookback_start, as_of, exclude_categories=annual_cats
    )

    canonical_names: dict[str, str] = {}
    variable_totals: dict[str, float] = {}
    for raw_cat, amt, cur, fx in variable_rows:
        cat_name = (raw_cat or "General").strip() or "General"
        key = cat_name.lower()
        canonical_names.setdefault(key, cat_name)
        variable_totals[key] = variable_totals.get(key, 0.0) + to_nzd_amount(amt, cur, fx)

    annual_rows = _fetch_specific_category_expense_rows(
        db, as_of - timedelta(days=DAYS_PER_YEAR), as_of, annual_cats
    )
    annual_totals: dict[str, float] = {}
    for raw_cat, amt, cur, fx in annual_rows:
        cat_name = (raw_cat or "General").strip() or "General"
        key = cat_name.lower()
        canonical_names.setdefault(key, cat_name)
        annual_totals[key] = annual_totals.get(key, 0.0) + to_nzd_amount(amt, cur, fx)

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

    estimates = [
        CategoryBurnEstimate(
            category=display_name,
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
                    is_active=True,
                )
            )

    db.commit()
    return list(db.scalars(select(BudgetRule).order_by(BudgetRule.category)).all())
