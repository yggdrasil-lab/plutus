"""Budget rule lifecycle operations, batched baseline seeding, and burn sync."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import BudgetRule, Category
from app.services.budget_rules._types import (
    DEFAULT_BASELINE_BUDGETS,
    BudgetRuleNotFoundError,
    BudgetRuleSnapshot,
    BudgetRuleUpsertInput,
    BudgetRuleValidationError,
    normalize_budget_category,
    resolve_budget_limits,
    snapshot_from_budget_rule,
)
from app.services.category_taxonomy import (
    CategoryTaxonomyError,
    load_categories_by_lower,
    resolve_budget_rule_period,
    sync_budget_rule_amortization,
)
from app.services.spending_projection import (
    MIN_SIGNIFICANT_WEEKLY_BURN_NZD,
    CategoryBurnEstimate,
    project_burn,
)


def _load_all_rules_sorted(db: Session) -> list[BudgetRule]:
    return list(db.scalars(select(BudgetRule).order_by(BudgetRule.category)).all())


def _sync_cadence_and_amortization(
    db: Session,
    *,
    category_name: str,
    is_active: bool,
    explicit_period: Optional[str] = None,
    existing_period: Optional[str] = None,
    existing_is_active: bool = True,
    annual_limit_only: bool = False,
    categories_by_lower: Optional[Mapping[str, Category]] = None,
    flush: bool = True,
) -> tuple[str, str]:
    try:
        resolved_period = resolve_budget_rule_period(
            db,
            category_name,
            explicit_period=explicit_period,
            existing_period=existing_period,
            existing_is_active=existing_is_active,
            annual_limit_only=annual_limit_only,
            categories_by_lower=categories_by_lower,
        )
        canonical_cat = sync_budget_rule_amortization(
            db,
            category_name=category_name,
            budget_period=resolved_period,
            is_active=is_active,
            categories_by_lower=categories_by_lower,
            flush=flush,
        )
    except CategoryTaxonomyError as exc:
        raise BudgetRuleValidationError(str(exc)) from exc
    return canonical_cat, resolved_period


def _apply_budget_rule_fields(
    rule: BudgetRule,
    *,
    category: str,
    weekly_limit: float,
    monthly_limit: float,
    budget_period: str,
    is_active: bool,
) -> None:
    rule.category = category
    rule.weekly_limit = weekly_limit
    rule.monthly_limit = monthly_limit
    rule.budget_period = budget_period
    rule.is_active = is_active


def _save_budget_rule_orm(
    db: Session,
    *,
    existing: Optional[BudgetRule],
    category: str,
    weekly_limit: float,
    monthly_limit: float,
    budget_period: str,
    is_active: bool,
) -> BudgetRule:
    if existing is not None:
        _apply_budget_rule_fields(
            existing,
            category=category,
            weekly_limit=weekly_limit,
            monthly_limit=monthly_limit,
            budget_period=budget_period,
            is_active=is_active,
        )
        return existing
    rule = BudgetRule(
        category=category,
        weekly_limit=weekly_limit,
        monthly_limit=monthly_limit,
        budget_period=budget_period,
        is_active=is_active,
    )
    db.add(rule)
    return rule


def list_budget_rules(
    db: Session,
    *,
    is_active: Optional[bool] = True,
) -> tuple[BudgetRuleSnapshot, ...]:
    """List budget rules sorted by category name as detached frozen snapshots."""
    stmt = select(BudgetRule)
    if is_active is not None:
        stmt = stmt.where(BudgetRule.is_active == is_active)
    rows = db.scalars(stmt.order_by(BudgetRule.category)).all()
    return tuple(snapshot_from_budget_rule(rule) for rule in rows)


def upsert_budget_rule(
    db: Session,
    payload: BudgetRuleUpsertInput,
) -> BudgetRuleSnapshot:
    """Create or update a category budget rule and sync category amortization."""
    cleaned = normalize_budget_category(payload.category)
    weekly, monthly = resolve_budget_limits(
        payload.weekly_limit, payload.monthly_limit, payload.annual_limit
    )
    existing = db.scalar(
        select(BudgetRule).where(func.lower(func.trim(BudgetRule.category)) == cleaned.lower())
    )
    annual_only = (
        payload.annual_limit is not None
        and payload.weekly_limit is None
        and payload.monthly_limit is None
    )
    canonical_cat, resolved_period = _sync_cadence_and_amortization(
        db,
        category_name=cleaned,
        is_active=bool(payload.is_active),
        explicit_period=payload.budget_period,
        existing_period=existing.budget_period if existing is not None else None,
        existing_is_active=bool(existing.is_active) if existing is not None else True,
        annual_limit_only=annual_only,
    )
    rule = _save_budget_rule_orm(
        db,
        existing=existing,
        category=canonical_cat,
        weekly_limit=weekly,
        monthly_limit=monthly,
        budget_period=resolved_period,
        is_active=bool(payload.is_active),
    )
    db.commit()
    db.refresh(rule)
    return snapshot_from_budget_rule(rule)


def delete_budget_rule(db: Session, budget_id: int) -> None:
    """Delete an existing budget rule while preserving Category.amortize_period."""
    rule = db.get(BudgetRule, budget_id)
    if rule is None:
        raise BudgetRuleNotFoundError("Budget rule not found")
    db.delete(rule)
    db.commit()


def initialize_default_budgets(db: Session) -> tuple[BudgetRuleSnapshot, ...]:
    """Seed missing baseline category budgets using batched lookups and one commit."""
    existing_rules = _load_all_rules_sorted(db)
    existing_by_lower = {rule.category.strip().lower(): rule for rule in existing_rules}
    missing_specs = [
        spec
        for spec in DEFAULT_BASELINE_BUDGETS
        if spec.category.lower() not in existing_by_lower
    ]
    if not missing_specs:
        return tuple(snapshot_from_budget_rule(rule) for rule in existing_rules)

    categories_by_lower = load_categories_by_lower(
        db, [spec.category for spec in missing_specs]
    )
    for spec in missing_specs:
        canonical_cat, resolved_period = _sync_cadence_and_amortization(
            db,
            category_name=spec.category,
            is_active=True,
            categories_by_lower=categories_by_lower,
            flush=False,
        )
        db.add(
            BudgetRule(
                category=canonical_cat,
                weekly_limit=spec.weekly_limit,
                monthly_limit=spec.monthly_limit,
                budget_period=resolved_period,
                is_active=True,
            )
        )
    db.commit()
    return tuple(snapshot_from_budget_rule(rule) for rule in _load_all_rules_sorted(db))


def _upsert_burn_estimates(
    db: Session,
    *,
    significant: list[CategoryBurnEstimate],
    existing_by_lower: dict[str, BudgetRule],
    categories_by_lower: Mapping[str, Category],
) -> None:
    for est in significant:
        canonical_cat, resolved_period = _sync_cadence_and_amortization(
            db,
            category_name=est.category,
            is_active=True,
            explicit_period=est.budget_period or "monthly",
            categories_by_lower=categories_by_lower,
            flush=False,
        )
        rule = _save_budget_rule_orm(
            db,
            existing=existing_by_lower.get(canonical_cat.lower()),
            category=canonical_cat,
            weekly_limit=est.average_weekly_burn,
            monthly_limit=est.suggested_monthly_limit,
            budget_period=resolved_period,
            is_active=True,
        )
        existing_by_lower[canonical_cat.lower()] = rule


def sync_budget_rules_from_burn(
    db: Session,
    *,
    lookback_weeks: int = 12,
    reference_date: Optional[date] = None,
) -> tuple[BudgetRuleSnapshot, ...]:
    """Upsert active category budget rules from projected average weekly burn."""
    if isinstance(lookback_weeks, bool) or not isinstance(lookback_weeks, int) or not (
        1 <= lookback_weeks <= 52
    ):
        raise BudgetRuleValidationError("lookback_weeks must be an integer between 1 and 52.")

    burn = project_burn(
        db,
        lookback_weeks=lookback_weeks,
        reference_date=reference_date or date.today(),
    )
    significant = [
        est
        for est in burn.categories
        if est.average_weekly_burn >= MIN_SIGNIFICANT_WEEKLY_BURN_NZD
    ]
    existing_rules = _load_all_rules_sorted(db)
    if not significant:
        return tuple(snapshot_from_budget_rule(rule) for rule in existing_rules)

    existing_by_lower = {rule.category.strip().lower(): rule for rule in existing_rules}
    categories_by_lower = load_categories_by_lower(
        db, [est.category for est in significant]
    )
    _upsert_burn_estimates(
        db,
        significant=significant,
        existing_by_lower=existing_by_lower,
        categories_by_lower=categories_by_lower,
    )
    db.commit()
    return tuple(snapshot_from_budget_rule(rule) for rule in _load_all_rules_sorted(db))
