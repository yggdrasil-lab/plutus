"""Bidirectional synchronization between BudgetRule cadence and Category amortization."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
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


def load_categories_by_lower(
    db: Session,
    category_names: Iterable[str],
) -> dict[str, Category]:
    """Batch-load Category rows keyed by trimmed lowercase name in one SELECT."""
    lowered_names = sorted(
        {
            cleaned.lower()
            for raw in category_names
            if (cleaned := (raw or "").strip())
        }
    )
    if not lowered_names:
        return {}
    rows = db.scalars(
        select(Category).where(func.lower(func.trim(Category.name)).in_(lowered_names))
    ).all()
    return {cat.name.strip().lower(): cat for cat in rows}


def resolve_budget_rule_category(
    db: Session,
    category_name: str,
    *,
    budget_period: str,
    categories_by_lower: Optional[Mapping[str, Category]] = None,
) -> str:
    """Validate budget category & period and return canonical casing without mutating Category."""
    canonical_name, _, _ = _resolve_validated_target(
        db,
        category_name,
        budget_period=budget_period,
        categories_by_lower=categories_by_lower,
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
    categories_by_lower: Optional[Mapping[str, Category]] = None,
) -> str:
    """Resolve effective budget_period from payload, active rule, annual_limit, or Category."""
    cleaned = normalize_category_name(category_name)
    if explicit_period is not None:
        return normalize_budget_period_setting(explicit_period)
    if existing_period is not None and existing_is_active:
        return normalize_budget_period_setting(existing_period)
    if annual_limit_only:
        return "annual"
    cat = _lookup_category_by_name(db, cleaned, categories_by_lower=categories_by_lower)
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
    categories_by_lower: Optional[Mapping[str, Category]] = None,
    flush: bool = True,
) -> str:
    """Validate category, sync Category.amortize_period when active, and return canonical name."""
    canonical_name, cat, normalized_period = _resolve_validated_target(
        db,
        category_name,
        budget_period=budget_period,
        categories_by_lower=categories_by_lower,
    )
    if is_active and cat is not None and not cat.is_system:
        cat.amortize_period = "annual" if normalized_period == "annual" else None
        if flush:
            db.flush()
    return canonical_name


def _lookup_category_by_name(
    db: Session,
    cleaned_name: str,
    *,
    categories_by_lower: Optional[Mapping[str, Category]] = None,
) -> Optional[Category]:
    if categories_by_lower is not None:
        return categories_by_lower.get(cleaned_name.lower())
    return db.scalar(
        select(Category).where(func.lower(func.trim(Category.name)) == cleaned_name.lower())
    )


def _resolve_validated_target(
    db: Session,
    category_name: str,
    *,
    budget_period: str,
    categories_by_lower: Optional[Mapping[str, Category]] = None,
) -> tuple[str, Optional[Category], str]:
    cleaned = normalize_category_name(category_name)
    normalized_period = normalize_budget_period_setting(budget_period)
    cat = _lookup_category_by_name(db, cleaned, categories_by_lower=categories_by_lower)
    if normalized_period == "annual" and (
        (cat is not None and cat.is_system) or is_system_category_name(cleaned)
    ):
        raise CategoryValidationError("System categories cannot use an annual budget period.")
    canonical_name = cat.name if cat is not None else cleaned
    return canonical_name, cat, normalized_period
