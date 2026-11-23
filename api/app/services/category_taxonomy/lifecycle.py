"""Category creation, update, six-table cascades, and deletion lifecycle."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    BudgetRule,
    CategorizationRule,
    Category,
    Product,
    ReceiptItem,
    Transaction,
    TransactionSplit,
)
from app.services.category_taxonomy._types import (
    UNSET,
    CategoryConflictError,
    CategoryCreateInput,
    CategoryDeleteSummary,
    CategoryNotFoundError,
    CategorySnapshot,
    CategoryUpdateInput,
    CategoryValidationError,
    _UnsetType,
    is_system_category_name,
    normalize_amortize_setting,
    normalize_category_name,
)
from app.services.category_taxonomy.queries import (
    ensure_default_categories,
    get_category_snapshot,
)

_CASCADE_TEXT_TARGETS: tuple[tuple[str, type[Any], Any], ...] = (
    ("transactions", Transaction, Transaction.category),
    ("splits", TransactionSplit, TransactionSplit.category),
    ("rules", CategorizationRule, CategorizationRule.target_category),
    ("receipt_items", ReceiptItem, ReceiptItem.category),
    ("products", Product, Product.category),
)
_MAX_SORT_ORDER = 10000


def _has_active_children(db: Session, category_id: int) -> bool:
    return (
        db.scalar(
            select(Category.id)
            .where(Category.parent_id == category_id, Category.is_active == True)
            .limit(1)
        )
        is not None
    )


def _validate_sort_order(sort_order: Optional[int]) -> Optional[int]:
    if sort_order is None:
        return None
    if sort_order < 0 or sort_order > _MAX_SORT_ORDER:
        raise CategoryValidationError(f"sort_order must be between 0 and {_MAX_SORT_ORDER}.")
    return sort_order


def _validate_parent_target(
    db: Session, parent_id: int, *, self_id: Optional[int] = None
) -> Category:
    if self_id is not None and parent_id == self_id:
        raise CategoryValidationError("A category cannot be its own parent.")
    parent = db.get(Category, parent_id)
    if parent is None:
        raise CategoryNotFoundError("Parent category not found.")
    if not parent.is_active:
        raise CategoryValidationError("Cannot nest under an inactive category.")
    if parent.is_system:
        raise CategoryValidationError("Cannot nest a category under a system category.")
    if parent.parent_id is not None:
        raise CategoryValidationError(
            "Only 2-level category hierarchy is supported; choose a base category as parent."
        )
    return parent


def _reassign_category_text_targets(
    db: Session, *, old_name: str, new_name: str
) -> dict[str, int]:
    old_lower = old_name.strip().lower()
    counts: dict[str, int] = {}
    for key, model, column in _CASCADE_TEXT_TARGETS:
        updated = (
            db.query(model)
            .filter(func.lower(func.trim(column)) == old_lower)
            .update({column: new_name}, synchronize_session="fetch")
        )
        counts[key] = int(updated or 0)
    return counts


def _cascade_budget_rule_rename(
    db: Session, *, old_name: str, new_name: str
) -> None:
    old_lower = old_name.strip().lower()
    new_lower = new_name.strip().lower()
    if old_lower == new_lower:
        existing_rules = list(
            db.scalars(
                select(BudgetRule)
                .where(func.lower(func.trim(BudgetRule.category)) == old_lower)
                .order_by(BudgetRule.id.asc())
            ).all()
        )
        if not existing_rules:
            return
        for duplicate in existing_rules[1:]:
            db.delete(duplicate)
        if len(existing_rules) > 1:
            db.flush()
        existing_rules[0].category = new_name
        db.flush()
        return

    source_rule = db.scalar(
        select(BudgetRule).where(func.lower(func.trim(BudgetRule.category)) == old_lower)
    )
    target_rule = db.scalar(
        select(BudgetRule).where(func.lower(func.trim(BudgetRule.category)) == new_lower)
    )
    if target_rule is not None:
        if source_rule is not None:
            db.delete(source_rule)
            db.flush()
        target_rule.category = new_name
        db.flush()
        return
    if source_rule is not None:
        source_rule.category = new_name
        db.flush()


def _deactivate_matching_budget_rule(db: Session, category_name: str) -> None:
    matching_rule = db.scalar(
        select(BudgetRule).where(
            func.lower(func.trim(BudgetRule.category)) == category_name.strip().lower()
        )
    )
    if matching_rule is not None:
        matching_rule.is_active = False
        db.flush()


def _sync_active_budget_rule_amortization(
    db: Session, *, category_name: str, amortize_period: Optional[str]
) -> None:
    matching_rule = db.scalar(
        select(BudgetRule).where(
            func.lower(func.trim(BudgetRule.category)) == category_name.strip().lower()
        )
    )
    if matching_rule is None or not matching_rule.is_active:
        return
    if amortize_period == "annual":
        matching_rule.budget_period = "annual"
    elif (matching_rule.budget_period or "").strip().lower() == "annual":
        matching_rule.budget_period = "monthly"


def _sync_create_budget_rule(
    db: Session, *, category_name: str, amortize_period: Optional[str]
) -> Optional[str]:
    matching_rule = db.scalar(
        select(BudgetRule).where(
            func.lower(func.trim(BudgetRule.category)) == category_name.strip().lower()
        )
    )
    if matching_rule is None or not matching_rule.is_active:
        return amortize_period
    matching_rule.category = category_name
    rule_period = (matching_rule.budget_period or "").strip().lower()
    if amortize_period == "annual":
        matching_rule.budget_period = "annual"
        return "annual"
    if rule_period == "annual":
        return "annual"
    return amortize_period


def _validate_system_update(
    cat: Category,
    *,
    cleaned_name: Optional[str],
    payload: CategoryUpdateInput,
    resolved_amortize: str | None | _UnsetType,
) -> None:
    if not cat.is_system:
        return
    if cleaned_name is not None and cleaned_name != cat.name:
        raise CategoryValidationError("System categories cannot be renamed.")
    if payload.parent_id is not UNSET and payload.parent_id != cat.parent_id:
        raise CategoryValidationError("System categories cannot be reparented.")
    if payload.is_active is False:
        raise CategoryValidationError("System categories cannot be deactivated.")
    if resolved_amortize is not UNSET and resolved_amortize is not None:
        raise CategoryValidationError("System categories cannot be amortized.")


def _validate_update_preconditions(
    db: Session, cat: Category, payload: CategoryUpdateInput
) -> tuple[Optional[str], str | None | _UnsetType]:
    cleaned_name = normalize_category_name(payload.name) if payload.name is not None else None
    resolved_amortize: str | None | _UnsetType = (
        normalize_amortize_setting(payload.amortize_period)
        if payload.amortize_period is not UNSET
        else UNSET
    )
    _validate_sort_order(payload.sort_order)
    _validate_system_update(
        cat, cleaned_name=cleaned_name, payload=payload, resolved_amortize=resolved_amortize
    )
    if cleaned_name is not None and cleaned_name != cat.name:
        conflict = db.scalar(
            select(Category).where(
                func.lower(Category.name) == cleaned_name.lower(), Category.id != cat.id
            )
        )
        if conflict is not None:
            raise CategoryConflictError(f"Category '{conflict.name}' already exists.")
    if payload.parent_id is not UNSET and payload.parent_id is not None:
        if _has_active_children(db, cat.id):
            raise CategoryValidationError(
                "Cannot move a base category with sub-categories underneath another parent."
            )
        _validate_parent_target(db, payload.parent_id, self_id=cat.id)
    if payload.is_active is False and _has_active_children(db, cat.id):
        raise CategoryValidationError(
            "Cannot deactivate a category that still has active sub-categories."
        )
    effective_parent_id = (
        payload.parent_id if payload.parent_id is not UNSET else cat.parent_id
    )
    if payload.is_active is True and not cat.is_active and effective_parent_id is not None:
        _validate_parent_target(db, effective_parent_id, self_id=cat.id)
    return cleaned_name, resolved_amortize


def _resolve_delete_target(
    db: Session, cat: Category, reassign_to: Optional[str]
) -> Category:
    cleaned_target = (reassign_to or "").strip()
    if cleaned_target:
        target_cat = db.scalar(
            select(Category).where(func.lower(func.trim(Category.name)) == cleaned_target.lower())
        )
        if target_cat is None or not target_cat.is_active:
            raise CategoryValidationError(
                f"Target reassignment category '{reassign_to}' not found."
            )
        if target_cat.id == cat.id:
            raise CategoryValidationError(
                "Cannot reassign records to the category being deleted."
            )
        return target_cat

    if cat.parent_id is not None:
        parent_cat = db.get(Category, cat.parent_id)
        if parent_cat is not None and parent_cat.is_active and parent_cat.id != cat.id:
            return parent_cat

    fallback = db.scalar(
        select(Category).where(func.lower(func.trim(Category.name)) == "general")
    )
    if fallback is None or not fallback.is_active or fallback.id == cat.id:
        raise CategoryValidationError(
            "No valid fallback category ('General') available for reassignment."
        )
    return fallback


def create_category(db: Session, payload: CategoryCreateInput) -> CategorySnapshot:
    """Create a root category or subcategory and sync any existing active BudgetRule."""
    cleaned_name = normalize_category_name(payload.name)
    amortize_period = normalize_amortize_setting(payload.amortize_period)
    _validate_sort_order(payload.sort_order)
    if amortize_period == "annual" and is_system_category_name(cleaned_name):
        raise CategoryValidationError("System categories cannot be amortized.")

    ensure_default_categories(db)
    existing = db.scalar(
        select(Category).where(func.lower(Category.name) == cleaned_name.lower())
    )
    if existing is not None:
        raise CategoryConflictError(f"Category '{existing.name}' already exists.")

    parent = (
        _validate_parent_target(db, payload.parent_id)
        if payload.parent_id is not None
        else None
    )
    resolved_sort = (
        payload.sort_order
        if payload.sort_order is not None
        else ((parent.sort_order + 5) if parent is not None else 150)
    )
    resolved_amortize = _sync_create_budget_rule(
        db, category_name=cleaned_name, amortize_period=amortize_period
    )
    cat = Category(
        name=cleaned_name,
        parent_id=parent.id if parent is not None else None,
        is_system=False,
        is_active=True,
        amortize_period=resolved_amortize,
        sort_order=resolved_sort,
    )
    db.add(cat)
    db.commit()
    return get_category_snapshot(db, cat.id)


def update_category(
    db: Session, category_id: int, payload: CategoryUpdateInput
) -> CategorySnapshot:
    """Rename, reparent, deactivate, or update amortization with 6-table cascade sync."""
    cat = db.get(Category, category_id)
    if cat is None:
        raise CategoryNotFoundError("Category not found.")

    cleaned_name, resolved_amortize = _validate_update_preconditions(db, cat, payload)
    if cleaned_name is not None and cleaned_name != cat.name:
        old_name = cat.name
        cat.name = cleaned_name
        _reassign_category_text_targets(db, old_name=old_name, new_name=cleaned_name)
        _cascade_budget_rule_rename(db, old_name=old_name, new_name=cleaned_name)

    if payload.parent_id is not UNSET:
        cat.parent_id = payload.parent_id
    if payload.is_active is not None:
        cat.is_active = payload.is_active
        if payload.is_active is False:
            _deactivate_matching_budget_rule(db, cat.name)
    if resolved_amortize is not UNSET:
        cat.amortize_period = resolved_amortize
        _sync_active_budget_rule_amortization(
            db, category_name=cat.name, amortize_period=resolved_amortize
        )
    if payload.sort_order is not None:
        cat.sort_order = payload.sort_order

    db.commit()
    return get_category_snapshot(db, cat.id)


def delete_category(
    db: Session, category_id: int, *, reassign_to: Optional[str] = None
) -> CategoryDeleteSummary:
    """Delete a non-system category and cascade reassignments across all six tables."""
    cat = db.get(Category, category_id)
    if cat is None:
        raise CategoryNotFoundError("Category not found.")
    if cat.is_system:
        raise CategoryValidationError("System categories cannot be deleted.")

    target_cat = _resolve_delete_target(db, cat, reassign_to)
    children = list(db.scalars(select(Category).where(Category.parent_id == cat.id)).all())
    for child in children:
        child.parent_id = None

    counts = _reassign_category_text_targets(
        db, old_name=cat.name, new_name=target_cat.name
    )
    old_lower = cat.name.strip().lower()
    db.query(BudgetRule).filter(func.lower(func.trim(BudgetRule.category)) == old_lower).delete(
        synchronize_session="fetch"
    )
    deleted_name = cat.name
    db.delete(cat)
    db.commit()

    return CategoryDeleteSummary(
        deleted_id=category_id,
        deleted_name=deleted_name,
        reassigned_to=target_cat.name,
        transactions_reassigned=counts["transactions"],
        splits_reassigned=counts["splits"],
        rules_reassigned=counts["rules"],
        receipt_items_reassigned=counts["receipt_items"],
        products_reassigned=counts["products"],
        children_promoted=len(children),
    )
