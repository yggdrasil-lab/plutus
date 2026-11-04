"""Mathematical financial runway and freedom horizon projection engine."""

import math
from typing import Optional


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
