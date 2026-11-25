"""Pure domain constants, exceptions, and immutable contracts for Budget Rules."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Optional

from app.models import BudgetRule
from app.services.spending_projection import (
    WEEKS_PER_YEAR,
    annual_to_weekly,
    monthly_to_weekly,
    weekly_to_monthly,
)

DEFAULT_WEEKLY_BUDGET_LIMIT_NZD: float = 100.0
MAX_BUDGET_CATEGORY_LENGTH: int = 50


class BudgetRuleError(ValueError):
    """Base domain exception for budget rule operations."""


class BudgetRuleValidationError(BudgetRuleError):
    """Raised when a budget rule input or taxonomy invariant is violated."""


class BudgetRuleNotFoundError(BudgetRuleError):
    """Raised when a target budget rule cannot be found."""


@dataclass(frozen=True)
class BaselineBudgetSpec:
    """Default baseline weekly and monthly budget target for a category."""

    category: str
    weekly_limit: float
    monthly_limit: float


DEFAULT_BASELINE_BUDGETS: tuple[BaselineBudgetSpec, ...] = (
    BaselineBudgetSpec("Groceries", 200.0, 866.67),
    BaselineBudgetSpec("Food", 100.0, 433.33),
    BaselineBudgetSpec("Transport", 60.0, 260.00),
    BaselineBudgetSpec("Utilities", 80.0, 346.67),
    BaselineBudgetSpec("Health", 50.0, 216.67),
    BaselineBudgetSpec("Leisure", 50.0, 216.67),
    BaselineBudgetSpec("Insurance", 55.0, 238.33),
)


@dataclass(frozen=True)
class BudgetRuleUpsertInput:
    """Typed input contract for creating or updating a budget rule."""

    category: str
    weekly_limit: Optional[float] = None
    monthly_limit: Optional[float] = None
    annual_limit: Optional[float] = None
    budget_period: Optional[str] = None
    is_active: bool = True


@dataclass(frozen=True)
class BudgetRuleSnapshot:
    """Detached immutable view of a persisted BudgetRule."""

    id: int
    category: str
    weekly_limit: float
    monthly_limit: float
    annual_limit: float
    budget_period: str
    is_active: bool


def snapshot_from_budget_rule(rule: BudgetRule) -> BudgetRuleSnapshot:
    """Materialize a detached frozen snapshot from an ORM BudgetRule row."""
    weekly = float(rule.weekly_limit)
    monthly = float(rule.monthly_limit)
    return BudgetRuleSnapshot(
        id=int(rule.id),
        category=str(rule.category),
        weekly_limit=weekly,
        monthly_limit=monthly,
        annual_limit=round(weekly * float(WEEKS_PER_YEAR), 2),
        budget_period=str(rule.budget_period),
        is_active=bool(rule.is_active),
    )


def normalize_budget_category(category: str) -> str:
    """Validate and trim a budget rule category name."""
    if not isinstance(category, str):
        raise BudgetRuleValidationError("Category name must be a string.")
    cleaned = category.strip()
    if not cleaned:
        raise BudgetRuleValidationError("Category name cannot be empty.")
    if len(cleaned) > MAX_BUDGET_CATEGORY_LENGTH:
        raise BudgetRuleValidationError(
            f"Category name must be {MAX_BUDGET_CATEGORY_LENGTH} characters or fewer."
        )
    return cleaned


def _validate_limit(value: Optional[float], field_name: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise BudgetRuleValidationError(f"{field_name} must be a non-negative finite number.")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise BudgetRuleValidationError(
            f"{field_name} must be a non-negative finite number."
        ) from exc
    if not math.isfinite(numeric) or numeric < 0.0:
        raise BudgetRuleValidationError(f"{field_name} must be a non-negative finite number.")
    return numeric


def resolve_budget_limits(
    weekly: Optional[float],
    monthly: Optional[float],
    annual: Optional[float] = None,
) -> tuple[float, float]:
    """Validate and resolve paired (weekly_limit, monthly_limit) values."""
    w = _validate_limit(weekly, "weekly_limit")
    m = _validate_limit(monthly, "monthly_limit")
    a = _validate_limit(annual, "annual_limit")
    if a is not None:
        resolved_weekly = w if w is not None else annual_to_weekly(a)
        resolved_monthly = m if m is not None else round(a / 12.0, 2)
        return resolved_weekly, resolved_monthly
    if w is None and m is None:
        return DEFAULT_WEEKLY_BUDGET_LIMIT_NZD, weekly_to_monthly(DEFAULT_WEEKLY_BUDGET_LIMIT_NZD)
    if w is not None and m is None:
        return w, weekly_to_monthly(w)
    if m is not None and w is None:
        return monthly_to_weekly(m), m
    assert w is not None and m is not None
    return w, m
