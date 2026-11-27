"""Financial runway, batched balance-tier snapshot, and unified telemetry engine."""

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date
import math
from typing import Any, Optional

from sqlalchemy import and_, desc, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Account, AssetSnapshot
from app.schemas.accounts import REVOLVING_CREDIT_ACCOUNT_TYPES
from app.services.credit_telemetry import (
    CreditFxResolutionError,
    compute_account_credit_telemetry,
    is_revolving_credit_with_limit,
    resolve_credit_fx_rate,
)
from app.services.loan_service import get_loan_portfolio_summary
from app.services.spending_projection import BurnProjection, project_burn

BASELINE_ANNUAL_RETURN = 0.05


def get_baseline_annual_return() -> float:
    """Return the configured baseline real annual return assumption."""
    return float(settings.default_real_annual_return)
PERPETUAL_RUNWAY_WEEKS_SENTINEL = 9999.0
PERPETUAL_RUNWAY_YEARS_SENTINEL = 999.0
VALID_LIQUIDITY_TIERS = frozenset({"cash", "growth", "locked"})


@dataclass(frozen=True)
class TierLabels:
    """Resolved display labels for Cash, Growth, and Locked liquidity tiers."""

    cash: str
    growth: str
    locked: str

    def as_dict(self) -> dict[str, str]:
        return {"cash": self.cash, "growth": self.growth, "locked": self.locked}


@dataclass(frozen=True)
class AccountBalanceSnapshot:
    """Detached, immutable latest balance snapshot for one active account."""

    account_id: int
    account_name: str
    account_type: str
    tier: str
    currency: str
    snapshot_date: Optional[date]
    balance: float
    balance_nzd: float


@dataclass(frozen=True)
class CreditBackstopSnapshot:
    """Detached, immutable revolving credit limit and emergency backstop telemetry in NZD."""

    total_credit_limit_nzd: float = 0.0
    total_credit_owed_nzd: float = 0.0
    total_available_credit_nzd: float = 0.0
    credit_utilization_pct: float = 0.0
    backstop_weeks: float = 0.0
    credit_card_available_nzd: float = 0.0
    credit_line_available_nzd: float = 0.0
    account_count: int = 0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "total_credit_limit_nzd": self.total_credit_limit_nzd,
            "total_credit_owed_nzd": self.total_credit_owed_nzd,
            "total_available_credit_nzd": self.total_available_credit_nzd,
            "credit_utilization_pct": self.credit_utilization_pct,
            "backstop_weeks": self.backstop_weeks,
            "credit_card_available_nzd": self.credit_card_available_nzd,
            "credit_line_available_nzd": self.credit_line_available_nzd,
            "account_count": self.account_count,
        }


@dataclass(frozen=True)
class BalanceTierSnapshot:
    """Detached, immutable rollup of active account balances across liquidity tiers."""

    cash_balance: float
    growth_balance: float
    locked_balance: float
    total_liquid_balance: float
    tier_labels: TierLabels
    accounts: tuple[AccountBalanceSnapshot, ...]
    credit_backstop: CreditBackstopSnapshot = field(default_factory=CreditBackstopSnapshot)


@dataclass(frozen=True)
class RunwaySnapshot:
    """Unified, detached runway, burn, balance-tier, and net-worth telemetry snapshot."""

    as_of_date: date
    annual_return: float
    balances: BalanceTierSnapshot
    cash_balance: float
    growth_balance: float
    locked_balance: float
    total_liquid_balance: float
    total_liabilities: float
    net_worth: float
    emergency_weeks: float
    growth_weeks: Optional[float]
    locked_weeks: float
    primary_runway_weeks: float
    primary_runway_years: float
    is_perpetual: bool
    perpetual_threshold_weekly_income: float
    observed_weekly_burn: float
    effective_weekly_burn: float
    variable_weekly_burn: float
    recurring_weekly_burn: float
    this_week_variable_spend: float
    this_week_spend: float
    weekly_budget_target: float
    time_banked_this_week: float
    tier_labels: TierLabels
    loan_free_date: Optional[date] = None
    loan_weekly_relief: float = 0.0
    credit_backstop: CreditBackstopSnapshot = field(default_factory=CreditBackstopSnapshot)


@dataclass(frozen=True)
class ScenarioSimulation:
    """Detached, immutable result of simulating burn, lump-sum, return, and loan-payoff changes."""

    baseline: RunwaySnapshot
    weekly_burn_adjustment: float
    lump_sum_adjustment: float
    pay_off_loans: bool
    baseline_annual_return: float
    simulated_annual_return: float
    simulated_weekly_burn: float
    simulated_liquid_balance: float
    simulated_runway_weeks: Optional[float]
    simulated_runway_years: Optional[float]
    is_perpetual: bool
    delta_weeks_compared_to_baseline: Optional[float]
    loan_free_date: Optional[date]
    loan_weekly_relief: float
    remaining_liabilities: float
    simulated_net_worth: float


def calculate_weekly_growth_rate(annual_real_return: float) -> float:
    """Calculate equivalent weekly compounding growth rate from an annual return."""
    if annual_real_return <= 0.0:
        return 0.0
    return math.pow(1.0 + annual_real_return, 1.0 / 52.0) - 1.0


def calculate_emergency_runway(cash_assets: float, weekly_burn: float) -> float:
    """Calculate zero-growth immediate survival floor in weeks using liquid cash only."""
    if weekly_burn <= 0.0:
        return 0.0
    if cash_assets <= 0.0:
        return 0.0
    return round(cash_assets / weekly_burn, 2)


def calculate_compounding_runway(
    accessible_assets: float,
    weekly_burn: float,
    annual_real_return: float,
) -> tuple[Optional[float], bool]:
    """Calculate compounding financial runway horizon in weeks.

    Returns:
        tuple of (runway_weeks, is_perpetual)
        If portfolio yield exceeds or equals weekly burn rate, runway is perpetual (is_perpetual=True, runway_weeks=None).
        Otherwise returns the exact number of weeks until depletion.
    """
    if weekly_burn <= 0.0:
        return None, True
    if accessible_assets <= 0.0:
        return 0.0, False

    weekly_g = calculate_weekly_growth_rate(annual_real_return)

    if weekly_g <= 0.0:
        # Linear depletion when no growth is assumed
        return round(accessible_assets / weekly_burn, 2), False

    weekly_yield = accessible_assets * weekly_g
    if weekly_yield >= weekly_burn:
        # Perpetual Financial Independence
        return None, True

    # Annuity depletion formula: N = ln(B / (B - A0 * g)) / ln(1 + g)
    ratio = weekly_burn / (weekly_burn - weekly_yield)
    n_weeks = math.log(ratio) / math.log(1.0 + weekly_g)
    return round(n_weeks, 2), False


def calculate_time_banked(weekly_budget: float, actual_spend: float) -> float:
    """Calculate the fraction of freedom weeks banked (or consumed) relative to weekly budget.

    Positive result indicates weeks banked forward; negative indicates deficit.
    """
    if weekly_budget <= 0.0:
        return 0.0
    return round((weekly_budget - actual_spend) / weekly_budget, 2)


def get_balance_tier_snapshot(
    db: Session,
    *,
    as_of: Optional[date] = None,
) -> BalanceTierSnapshot:
    """Fetch active account balances and tier labels in one batched SQL query."""
    snapshot_stmt = select(
        AssetSnapshot.account_id,
        AssetSnapshot.snapshot_date,
        AssetSnapshot.balance,
        AssetSnapshot.balance_nzd,
        AssetSnapshot.exchange_rate,
        func.row_number()
        .over(
            partition_by=AssetSnapshot.account_id,
            order_by=(desc(AssetSnapshot.snapshot_date), desc(AssetSnapshot.id)),
        )
        .label("rn"),
    )
    if as_of is not None:
        snapshot_stmt = snapshot_stmt.where(AssetSnapshot.snapshot_date <= as_of)
    ranked_snapshots = snapshot_stmt.subquery("ranked_snapshots")

    stmt = (
        select(
            Account.id.label("account_id"),
            Account.name.label("account_name"),
            Account.account_type.label("account_type"),
            Account.tier.label("tier"),
            Account.currency.label("currency"),
            Account.credit_limit.label("credit_limit"),
            ranked_snapshots.c.snapshot_date.label("snapshot_date"),
            ranked_snapshots.c.balance.label("balance"),
            ranked_snapshots.c.balance_nzd.label("balance_nzd"),
            ranked_snapshots.c.exchange_rate.label("exchange_rate"),
        )
        .select_from(Account)
        .outerjoin(
            ranked_snapshots,
            and_(
                ranked_snapshots.c.account_id == Account.id,
                ranked_snapshots.c.rn == 1,
            ),
        )
        .where(Account.is_active == True)  # noqa: E712
        .order_by(Account.id)
    )
    rows = db.execute(stmt).all()
    return _build_balance_tier_snapshot(rows, db=db, rate_date=as_of or date.today())


def get_runway_snapshot(
    db: Session,
    *,
    as_of: Optional[date] = None,
    annual_return: Optional[float] = None,
) -> RunwaySnapshot:
    """Compute unified runway, burn, balance-tier, and net-worth telemetry."""
    effective_as_of = as_of or date.today()
    resolved_return = annual_return if annual_return is not None else get_baseline_annual_return()
    balances = get_balance_tier_snapshot(db, as_of=as_of)
    burn = project_burn(db, reference_date=effective_as_of)
    loan_summary = get_loan_portfolio_summary(db, as_of=effective_as_of)
    return _assemble_runway_snapshot(
        as_of_date=effective_as_of,
        annual_return=resolved_return,
        balances=balances,
        burn=burn,
        total_liabilities=loan_summary.total_liabilities,
        loan_free_date=loan_summary.latest_payoff_date,
        loan_weekly_relief=loan_summary.total_weekly_payment,
    )


def build_scenario_simulation(
    baseline: RunwaySnapshot,
    *,
    weekly_burn_adjustment: float = 0.0,
    custom_annual_return: Optional[float] = None,
    lump_sum_adjustment: float = 0.0,
    pay_off_loans: bool = False,
) -> ScenarioSimulation:
    """Simulate a what-if scenario purely from a detached baseline RunwaySnapshot."""
    burn_delta = weekly_burn_adjustment - (baseline.loan_weekly_relief if pay_off_loans else 0.0)
    liquid_delta = lump_sum_adjustment - (baseline.total_liabilities if pay_off_loans else 0.0)
    simulated_burn = max(1.0, baseline.effective_weekly_burn + burn_delta)
    simulated_liquid = max(0.0, baseline.total_liquid_balance + liquid_delta)
    simulated_return = (
        custom_annual_return if custom_annual_return is not None else baseline.annual_return
    )
    sim_weeks, is_perpetual = calculate_compounding_runway(
        accessible_assets=simulated_liquid,
        weekly_burn=simulated_burn,
        annual_real_return=simulated_return,
    )
    sim_years = round(sim_weeks / 52.0, 2) if sim_weeks is not None else None
    delta_weeks: Optional[float] = None
    if (
        not is_perpetual
        and not baseline.is_perpetual
        and sim_weeks is not None
        and baseline.growth_weeks is not None
    ):
        delta_weeks = round(sim_weeks - baseline.growth_weeks, 2)

    remaining_liabilities = 0.0 if pay_off_loans else baseline.total_liabilities
    simulated_net_worth = round(simulated_liquid + baseline.locked_balance - remaining_liabilities, 2)

    return ScenarioSimulation(
        baseline=baseline,
        weekly_burn_adjustment=weekly_burn_adjustment,
        lump_sum_adjustment=lump_sum_adjustment,
        pay_off_loans=pay_off_loans,
        baseline_annual_return=baseline.annual_return,
        simulated_annual_return=simulated_return,
        simulated_weekly_burn=simulated_burn,
        simulated_liquid_balance=simulated_liquid,
        simulated_runway_weeks=sim_weeks,
        simulated_runway_years=sim_years,
        is_perpetual=is_perpetual,
        delta_weeks_compared_to_baseline=delta_weeks,
        loan_free_date=baseline.loan_free_date,
        loan_weekly_relief=baseline.loan_weekly_relief,
        remaining_liabilities=remaining_liabilities,
        simulated_net_worth=simulated_net_worth,
    )


def simulate_runway_scenario(
    db: Session,
    *,
    as_of: Optional[date] = None,
    weekly_burn_adjustment: float = 0.0,
    custom_annual_return: Optional[float] = None,
    lump_sum_adjustment: float = 0.0,
    pay_off_loans: bool = False,
    baseline_annual_return: Optional[float] = None,
) -> ScenarioSimulation:
    """Load baseline runway telemetry and simulate a what-if scenario."""
    baseline = get_runway_snapshot(
        db,
        as_of=as_of,
        annual_return=baseline_annual_return,
    )
    return build_scenario_simulation(
        baseline,
        weekly_burn_adjustment=weekly_burn_adjustment,
        custom_annual_return=custom_annual_return,
        lump_sum_adjustment=lump_sum_adjustment,
        pay_off_loans=pay_off_loans,
    )


def _normalize_tier(raw_tier: Optional[str]) -> str:
    """Normalize account liquidity tier string, mapping unknown values to 'cash'."""
    cleaned = (raw_tier or "").strip().lower()
    return cleaned if cleaned in VALID_LIQUIDITY_TIERS else "cash"


def _resolve_tier_labels(accounts: Sequence[AccountBalanceSnapshot]) -> TierLabels:
    """Resolve display labels for Cash, Growth, and Locked tiers from active accounts."""
    cash_names = [
        acc.account_name
        for acc in accounts
        if acc.tier == "cash" and acc.account_type not in REVOLVING_CREDIT_ACCOUNT_TYPES
    ]
    growth_names = [acc.account_name for acc in accounts if acc.tier == "growth"]
    locked_names = [acc.account_name for acc in accounts if acc.tier == "locked"]
    return TierLabels(
        cash=cash_names[0] if len(cash_names) == 1 else "Cash Floor",
        growth=growth_names[0] if len(growth_names) == 1 else "Growth",
        locked=locked_names[0] if len(locked_names) == 1 else "Locked Reserve",
    )


def _resolve_row_credit_fx(
    row: Any,
    *,
    db: Optional[Session],
    rate_date: date,
    rate_cache: dict[tuple[str, date], Optional[float]],
) -> float:
    snap_fx = getattr(row, "exchange_rate", None)
    if db is not None:
        return resolve_credit_fx_rate(
            db,
            currency=str(row.currency),
            rate_date=rate_date,
            account_name=str(row.account_name),
            snapshot_exchange_rate=snap_fx,
            rate_cache=rate_cache,
        )
    if str(row.currency).strip().upper() == "NZD":
        return 1.0
    if snap_fx is not None and float(snap_fx) > 0.0 and float(snap_fx) != 1.0:
        return float(snap_fx)
    raise CreditFxResolutionError(f"Cannot resolve {row.currency}->NZD FX rate without DB session.")


def _build_credit_backstop_snapshot(
    rows: Sequence[Any],
    *,
    db: Optional[Session],
    rate_date: date,
) -> CreditBackstopSnapshot:
    """Compute detached NZD credit backstop totals across active revolving credit accounts."""
    limit_nzd_total = 0.0
    owed_nzd_total = 0.0
    available_nzd_total = 0.0
    card_available_nzd = 0.0
    line_available_nzd = 0.0
    account_count = 0
    rate_cache: dict[tuple[str, date], Optional[float]] = {}

    for row in rows:
        credit_limit = getattr(row, "credit_limit", None)
        if not is_revolving_credit_with_limit(row.account_type, credit_limit):
            continue
        fx_rate = _resolve_row_credit_fx(row, db=db, rate_date=rate_date, rate_cache=rate_cache)
        latest_balance = float(row.balance) if row.balance is not None else None
        telemetry = compute_account_credit_telemetry(
            account_type=str(row.account_type),
            credit_limit=float(credit_limit),
            latest_balance=latest_balance,
            fx_rate=fx_rate,
        )
        if telemetry is None:
            continue
        account_count += 1
        limit_nzd_total += telemetry.credit_limit_nzd
        owed_nzd_total += telemetry.credit_owed_nzd
        available_nzd_total += telemetry.available_credit_nzd
        if str(row.account_type).strip().lower() == "credit_card":
            card_available_nzd += telemetry.available_credit_nzd
        else:
            line_available_nzd += telemetry.available_credit_nzd

    rounded_limit = round(limit_nzd_total, 2)
    rounded_owed = round(owed_nzd_total, 2)
    utilization_pct = round((rounded_owed / rounded_limit) * 100.0, 2) if rounded_limit > 0.0 else 0.0
    return CreditBackstopSnapshot(
        total_credit_limit_nzd=rounded_limit,
        total_credit_owed_nzd=rounded_owed,
        total_available_credit_nzd=round(available_nzd_total, 2),
        credit_utilization_pct=utilization_pct,
        backstop_weeks=0.0,
        credit_card_available_nzd=round(card_available_nzd, 2),
        credit_line_available_nzd=round(line_available_nzd, 2),
        account_count=account_count,
    )


def _build_balance_tier_snapshot(
    rows: Sequence[Any],
    *,
    db: Optional[Session] = None,
    rate_date: Optional[date] = None,
) -> BalanceTierSnapshot:
    """Materialize detached account snapshots, rounded tier totals, and display labels."""
    totals: dict[str, float] = {tier: 0.0 for tier in VALID_LIQUIDITY_TIERS}
    accounts: list[AccountBalanceSnapshot] = []
    for row in rows:
        tier = _normalize_tier(row.tier)
        balance = float(row.balance) if row.balance is not None else 0.0
        balance_nzd = float(row.balance_nzd) if row.balance_nzd is not None else 0.0
        accounts.append(
            AccountBalanceSnapshot(
                account_id=int(row.account_id),
                account_name=str(row.account_name),
                account_type=str(row.account_type),
                tier=tier,
                currency=str(row.currency),
                snapshot_date=row.snapshot_date,
                balance=balance,
                balance_nzd=balance_nzd,
            )
        )
        totals[tier] += balance_nzd

    cash_balance = round(totals["cash"], 2)
    growth_balance = round(totals["growth"], 2)
    locked_balance = round(totals["locked"], 2)
    total_liquid_balance = round(cash_balance + growth_balance, 2)
    account_tuple = tuple(accounts)
    credit_backstop = _build_credit_backstop_snapshot(
        rows,
        db=db,
        rate_date=rate_date or date.today(),
    )
    return BalanceTierSnapshot(
        cash_balance=cash_balance,
        growth_balance=growth_balance,
        locked_balance=locked_balance,
        total_liquid_balance=total_liquid_balance,
        tier_labels=_resolve_tier_labels(account_tuple),
        accounts=account_tuple,
        credit_backstop=credit_backstop,
    )


def _assemble_runway_snapshot(
    *,
    as_of_date: date,
    annual_return: float,
    balances: BalanceTierSnapshot,
    burn: BurnProjection,
    total_liabilities: float,
    loan_free_date: Optional[date] = None,
    loan_weekly_relief: float = 0.0,
) -> RunwaySnapshot:
    """Assemble unified RunwaySnapshot from balance tiers, burn projection, and liabilities."""
    weekly_burn = burn.effective_weekly_burn
    emergency_weeks = calculate_emergency_runway(balances.cash_balance, weekly_burn)
    growth_weeks, is_perpetual = calculate_compounding_runway(
        accessible_assets=balances.total_liquid_balance,
        weekly_burn=weekly_burn,
        annual_real_return=annual_return,
    )
    perpetual_income = round(
        balances.total_liquid_balance * calculate_weekly_growth_rate(annual_return), 2
    )
    primary_weeks = (
        PERPETUAL_RUNWAY_WEEKS_SENTINEL if is_perpetual else (growth_weeks or emergency_weeks)
    )
    primary_years = (
        PERPETUAL_RUNWAY_YEARS_SENTINEL if is_perpetual else round(primary_weeks / 52.0, 2)
    )
    locked_weeks = round(balances.locked_balance / weekly_burn, 1) if weekly_burn > 0 else 0.0
    net_worth = round(
        balances.total_liquid_balance + balances.locked_balance - total_liabilities, 2
    )
    time_banked = calculate_time_banked(burn.weekly_budget_target, burn.this_week_spend)
    backstop_weeks = (
        round(balances.credit_backstop.total_available_credit_nzd / weekly_burn, 1)
        if weekly_burn > 0
        else 0.0
    )
    credit_backstop = replace(balances.credit_backstop, backstop_weeks=backstop_weeks)

    return RunwaySnapshot(
        as_of_date=as_of_date,
        annual_return=annual_return,
        balances=balances,
        cash_balance=balances.cash_balance,
        growth_balance=balances.growth_balance,
        locked_balance=balances.locked_balance,
        total_liquid_balance=balances.total_liquid_balance,
        total_liabilities=total_liabilities,
        net_worth=net_worth,
        emergency_weeks=emergency_weeks,
        growth_weeks=growth_weeks,
        locked_weeks=locked_weeks,
        primary_runway_weeks=primary_weeks,
        primary_runway_years=primary_years,
        is_perpetual=is_perpetual,
        perpetual_threshold_weekly_income=perpetual_income,
        observed_weekly_burn=burn.observed_weekly_burn,
        effective_weekly_burn=weekly_burn,
        variable_weekly_burn=burn.variable_weekly_burn,
        recurring_weekly_burn=burn.recurring_weekly_burn,
        this_week_variable_spend=burn.this_week_variable_spend,
        this_week_spend=burn.this_week_spend,
        weekly_budget_target=burn.weekly_budget_target,
        time_banked_this_week=time_banked,
        tier_labels=balances.tier_labels,
        loan_free_date=loan_free_date,
        loan_weekly_relief=loan_weekly_relief,
        credit_backstop=credit_backstop,
    )
