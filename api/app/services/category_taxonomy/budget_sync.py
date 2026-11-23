"""Bidirectional synchronization between BudgetRule cadence and Category amortization."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Category
from app.services.category_taxonomy._types import (
    CategoryValidationError,
    is_system_category_name,
    normalize_budget_period_setting,
    normalize_category_name,
)


def resolve_budget_rule_category(
    db: Session,
    category_name: str,
    *,
    budget_period: str,
) -> str:
    """Validate budget category & period and return canonical casing without mutating Category."""
    canonical_name, _, _ = _resolve_validated_target(
        db, category_name, budget_period=budget_period
    )
    return canonical_name


def resolve_budget_rule_period(
    db: Session,
    category_name: str,
    *,
    explicit_period: Optional[str] = None,
    existing_period: Optional[str] = None,
    existing_is_active: bool = True,
    annual_limit_only: bool = False,
) -> str:
    """Resolve effective budget_period from payload, active rule, annual_limit, or Category."""
    cleaned = normalize_category_name(category_name)
    if explicit_period is not None:
        return normalize_budget_period_setting(explicit_period)
    if existing_period is not None and existing_is_active:
        return normalize_budget_period_setting(existing_period)
    if annual_limit_only:
        return "annual"
    cat = _lookup_category_by_name(db, cleaned)
    if cat is not None and (cat.amortize_period or "").strip().lower() == "annual":
        return "annual"
    if existing_period is not None:
        return normalize_budget_period_setting(existing_period)
    return "monthly"


def sync_budget_rule_amortization(
    db: Session,
    *,
    category_name: str,
    budget_period: str,
    is_active: bool,
) -> str:
    """Validate category, sync Category.amortize_period when active, and return canonical name."""
    canonical_name, cat, normalized_period = _resolve_validated_target(
        db, category_name, budget_period=budget_period
    )
    if is_active and cat is not None and not cat.is_system:
        cat.amortize_period = "annual" if normalized_period == "annual" else None
        db.flush()
    return canonical_name


def _lookup_category_by_name(db: Session, cleaned_name: str) -> Optional[Category]:
    return db.scalar(
        select(Category).where(func.lower(func.trim(Category.name)) == cleaned_name.lower())
    )


def _resolve_validated_target(
    db: Session,
    category_name: str,
    *,
    budget_period: str,
) -> tuple[str, Optional[Category], str]:
    cleaned = normalize_category_name(category_name)
    normalized_period = normalize_budget_period_setting(budget_period)
    cat = _lookup_category_by_name(db, cleaned)
    if normalized_period == "annual" and (
        (cat is not None and cat.is_system) or is_system_category_name(cleaned)
    ):
        raise CategoryValidationError("System categories cannot use an annual budget period.")
    canonical_name = cat.name if cat is not None else cleaned
    return canonical_name, cat, normalized_period
