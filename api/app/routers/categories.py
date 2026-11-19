"""CRUD router for hierarchical spending categories and 52-week amortization settings."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    BudgetRule,
    CategorizationRule,
    Category,
    Product,
    ReceiptItem,
    Transaction,
)
from app.schemas import (
    CategoryCreate,
    CategoryDeleteResult,
    CategoryRead,
    CategoryUpdate,
)
from app.services.category_taxonomy import ensure_default_categories

router = APIRouter(prefix="/categories", tags=["Categories & Hierarchy"])


def _load_category_metrics(
    db: Session,
) -> tuple[dict[str, int], dict[str, int], dict[int, int], dict[str, BudgetRule]]:
    """Load transaction counts, rule counts, child counts, and active budget rules."""
    tx_counts = {
        (cat or "").strip().lower(): int(cnt)
        for cat, cnt in db.execute(
            select(func.lower(Transaction.category), func.count(Transaction.id)).group_by(
                func.lower(Transaction.category)
            )
        ).all()
        if cat
    }
    rule_counts = {
        (cat or "").strip().lower(): int(cnt)
        for cat, cnt in db.execute(
            select(
                func.lower(CategorizationRule.target_category),
                func.count(CategorizationRule.id),
            ).group_by(func.lower(CategorizationRule.target_category))
        ).all()
        if cat
    }
    child_counts = {
        int(parent_id): int(cnt)
        for parent_id, cnt in db.execute(
            select(Category.parent_id, func.count(Category.id))
            .where(Category.parent_id.is_not(None), Category.is_active == True)
            .group_by(Category.parent_id)
        ).all()
        if parent_id is not None
    }
    budget_rules = {
        (rule.category or "").strip().lower(): rule
        for rule in db.scalars(select(BudgetRule).where(BudgetRule.is_active == True)).all()
        if rule.category
    }
    return tx_counts, rule_counts, child_counts, budget_rules


def _serialize_category(
    cat: Category,
    *,
    by_id: dict[int, Category],
    tx_counts: dict[str, int],
    rule_counts: dict[str, int],
    child_counts: dict[int, int],
    budget_rules: dict[str, BudgetRule],
) -> CategoryRead:
    """Build a CategoryRead DTO enriched with parent info, counts, and budget metadata."""
    key = cat.name.strip().lower()
    parent = by_id.get(cat.parent_id) if cat.parent_id is not None else None
    rule = budget_rules.get(key)
    weekly = float(rule.weekly_limit) if rule is not None else None
    monthly = float(rule.monthly_limit) if rule is not None else None
    annual = round(weekly * 52.0, 2) if weekly is not None else None
    resolved_budget_period = (
        (rule.budget_period or "monthly").strip().lower()
        if rule is not None
        else ("annual" if cat.amortize_period == "annual" else None)
    )
    return CategoryRead(
        id=cat.id,
        name=cat.name,
        parent_id=cat.parent_id,
        parent_name=parent.name if parent is not None else None,
        is_system=cat.is_system,
        is_active=cat.is_active,
        amortize_period=cat.amortize_period,
        sort_order=cat.sort_order,
        transaction_count=tx_counts.get(key, 0),
        rule_count=rule_counts.get(key, 0),
        child_count=child_counts.get(cat.id, 0),
        has_budget_rule=rule is not None,
        budget_period=resolved_budget_period,
        weekly_limit=weekly,
        monthly_limit=monthly,
        annual_limit=annual,
    )


def _hierarchy_sort_key(cat: Category, by_id: dict[int, Category]) -> tuple:
    """Sort roots with their nested children grouped immediately underneath."""
    parent = by_id.get(cat.parent_id) if cat.parent_id is not None else None
    root = parent if parent is not None else cat
    return (
        1 if root.is_system else 0,
        root.sort_order,
        root.name.lower(),
        1 if parent is not None else 0,
        cat.sort_order,
        cat.name.lower(),
    )


def _get_category_read_by_id(db: Session, category_id: int) -> CategoryRead:
    all_cats = db.scalars(select(Category)).all()
    by_id = {c.id: c for c in all_cats}
    cat = by_id[category_id]
    tx_counts, rule_counts, child_counts, budget_rules = _load_category_metrics(db)
    return _serialize_category(
        cat,
        by_id=by_id,
        tx_counts=tx_counts,
        rule_counts=rule_counts,
        child_counts=child_counts,
        budget_rules=budget_rules,
    )


def _validate_parent_target(db: Session, parent_id: int, *, self_id: Optional[int] = None) -> Category:
    """Validate that a target parent exists, is active, is not system, and is a root class."""
    if self_id is not None and parent_id == self_id:
        raise HTTPException(status_code=400, detail="A category cannot be its own parent.")
    parent = db.get(Category, parent_id)
    if not parent:
        raise HTTPException(status_code=404, detail="Parent category not found.")
    if not parent.is_active:
        raise HTTPException(status_code=400, detail="Cannot nest under an inactive category.")
    if parent.is_system:
        raise HTTPException(
            status_code=400,
            detail="Cannot nest a category under a system category.",
        )
    if parent.parent_id is not None:
        raise HTTPException(
            status_code=400,
            detail="Only 2-level category hierarchy is supported; choose a base category as parent.",
        )
    return parent


@router.get("", response_model=list[CategoryRead])
def list_categories(
    include_inactive: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    """List hierarchical categories ordered by base class then nested sub-categories."""
    all_cats = ensure_default_categories(db)
    by_id = {c.id: c for c in all_cats}
    tx_counts, rule_counts, child_counts, budget_rules = _load_category_metrics(db)

    visible = [c for c in all_cats if include_inactive or c.is_active]
    visible.sort(key=lambda c: _hierarchy_sort_key(c, by_id))
    return [
        _serialize_category(
            c,
            by_id=by_id,
            tx_counts=tx_counts,
            rule_counts=rule_counts,
            child_counts=child_counts,
            budget_rules=budget_rules,
        )
        for c in visible
    ]


@router.post("", response_model=CategoryRead, status_code=status.HTTP_201_CREATED)
def create_category(
    payload: CategoryCreate,
    db: Session = Depends(get_db),
):
    """Create a new base category or sub-category with optional 52-week annual amortization."""
    ensure_default_categories(db)
    existing = db.scalar(
        select(Category).where(func.lower(Category.name) == payload.name.lower())
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Category '{existing.name}' already exists.",
        )

    parent: Optional[Category] = None
    if payload.parent_id is not None:
        parent = _validate_parent_target(db, payload.parent_id)

    resolved_sort = payload.sort_order
    if resolved_sort is None:
        resolved_sort = (parent.sort_order + 5) if parent is not None else 150

    cat = Category(
        name=payload.name,
        parent_id=parent.id if parent is not None else None,
        is_system=False,
        is_active=True,
        amortize_period=payload.amortize_period,
        sort_order=resolved_sort,
    )
    db.add(cat)
    db.flush()

    if payload.amortize_period == "annual":
        matching_rule = db.scalar(
            select(BudgetRule).where(func.lower(BudgetRule.category) == cat.name.lower())
        )
        if matching_rule and matching_rule.is_active:
            matching_rule.budget_period = "annual"

    db.commit()
    return _get_category_read_by_id(db, cat.id)


@router.patch("/{category_id}", response_model=CategoryRead)
def update_category(
    category_id: int,
    payload: CategoryUpdate,
    db: Session = Depends(get_db),
):
    """Rename, reparent, or toggle 52-week amortization on an existing category."""
    cat = db.get(Category, category_id)
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found.")

    if cat.is_system:
        if "name" in payload.model_fields_set and payload.name is not None and payload.name != cat.name:
            raise HTTPException(status_code=400, detail="System categories cannot be renamed.")
        if "parent_id" in payload.model_fields_set and payload.parent_id != cat.parent_id:
            raise HTTPException(status_code=400, detail="System categories cannot be reparented.")
        if "is_active" in payload.model_fields_set and payload.is_active is False:
            raise HTTPException(status_code=400, detail="System categories cannot be deactivated.")
        if "amortize_period" in payload.model_fields_set and payload.amortize_period is not None:
            raise HTTPException(status_code=400, detail="System categories cannot be amortized.")

    if "name" in payload.model_fields_set and payload.name is not None:
        old_name = cat.name
        new_name = payload.name
        if new_name != old_name:
            conflict = db.scalar(
                select(Category).where(
                    func.lower(Category.name) == new_name.lower(),
                    Category.id != cat.id,
                )
            )
            if conflict:
                raise HTTPException(
                    status_code=409,
                    detail=f"Category '{conflict.name}' already exists.",
                )
            cat.name = new_name
            old_lower = old_name.strip().lower()
            db.query(Transaction).filter(
                func.lower(Transaction.category) == old_lower
            ).update({Transaction.category: new_name}, synchronize_session=False)
            db.query(CategorizationRule).filter(
                func.lower(CategorizationRule.target_category) == old_lower
            ).update({CategorizationRule.target_category: new_name}, synchronize_session=False)
            db.query(ReceiptItem).filter(
                func.lower(ReceiptItem.category) == old_lower
            ).update({ReceiptItem.category: new_name}, synchronize_session=False)
            db.query(Product).filter(
                func.lower(Product.category) == old_lower
            ).update({Product.category: new_name}, synchronize_session=False)

            existing_new_rule = db.scalar(
                select(BudgetRule).where(func.lower(BudgetRule.category) == new_name.lower())
            )
            if existing_new_rule is None:
                db.query(BudgetRule).filter(
                    func.lower(BudgetRule.category) == old_lower
                ).update({BudgetRule.category: new_name}, synchronize_session=False)

    if "parent_id" in payload.model_fields_set:
        if payload.parent_id is not None:
            has_children = (
                db.scalar(
                    select(Category.id).where(Category.parent_id == cat.id).limit(1)
                )
                is not None
            )
            if has_children:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot move a base category with sub-categories underneath another parent.",
                )
            _validate_parent_target(db, payload.parent_id, self_id=cat.id)
        cat.parent_id = payload.parent_id

    if "amortize_period" in payload.model_fields_set:
        cat.amortize_period = payload.amortize_period
        matching_rule = db.scalar(
            select(BudgetRule).where(func.lower(BudgetRule.category) == cat.name.lower())
        )
        if matching_rule and matching_rule.is_active:
            if cat.amortize_period == "annual":
                matching_rule.budget_period = "annual"
            elif (matching_rule.budget_period or "").strip().lower() == "annual":
                matching_rule.budget_period = "monthly"

    if "is_active" in payload.model_fields_set and payload.is_active is not None:
        cat.is_active = payload.is_active

    if "sort_order" in payload.model_fields_set and payload.sort_order is not None:
        cat.sort_order = payload.sort_order

    db.commit()
    return _get_category_read_by_id(db, cat.id)


@router.delete("/{category_id}", response_model=CategoryDeleteResult)
def delete_category(
    category_id: int,
    reassign_to: Optional[str] = Query(
        default=None,
        description="Target category name for existing transactions and rules (defaults to parent or General)",
    ),
    db: Session = Depends(get_db),
):
    """Delete a non-system category and safely reassign linked transactions, rules, and items."""
    cat = db.get(Category, category_id)
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found.")
    if cat.is_system:
        raise HTTPException(status_code=400, detail="System categories cannot be deleted.")

    target_cat: Optional[Category] = None
    if reassign_to and reassign_to.strip():
        target_cat = db.scalar(
            select(Category).where(func.lower(Category.name) == reassign_to.strip().lower())
        )
        if not target_cat or not target_cat.is_active:
            raise HTTPException(
                status_code=400,
                detail=f"Target reassignment category '{reassign_to}' not found.",
            )
        if target_cat.id == cat.id:
            raise HTTPException(
                status_code=400,
                detail="Cannot reassign records to the category being deleted.",
            )
    else:
        if cat.parent_id is not None:
            parent_cat = db.get(Category, cat.parent_id)
            if parent_cat and parent_cat.is_active and parent_cat.id != cat.id:
                target_cat = parent_cat
        if target_cat is None:
            target_cat = db.scalar(
                select(Category).where(func.lower(Category.name) == "general")
            )
        if target_cat is None or target_cat.id == cat.id:
            raise HTTPException(
                status_code=400,
                detail="No valid fallback category ('General') available for reassignment.",
            )

    target_name = target_cat.name
    old_lower = cat.name.strip().lower()

    children = db.scalars(select(Category).where(Category.parent_id == cat.id)).all()
    children_promoted = 0
    for child in children:
        child.parent_id = None
        children_promoted += 1

    tx_updated = int(
        db.query(Transaction)
        .filter(func.lower(Transaction.category) == old_lower)
        .update({Transaction.category: target_name}, synchronize_session=False)
    )
    rule_updated = int(
        db.query(CategorizationRule)
        .filter(func.lower(CategorizationRule.target_category) == old_lower)
        .update({CategorizationRule.target_category: target_name}, synchronize_session=False)
    )
    item_updated = int(
        db.query(ReceiptItem)
        .filter(func.lower(ReceiptItem.category) == old_lower)
        .update({ReceiptItem.category: target_name}, synchronize_session=False)
    )
    prod_updated = int(
        db.query(Product)
        .filter(func.lower(Product.category) == old_lower)
        .update({Product.category: target_name}, synchronize_session=False)
    )
    db.query(BudgetRule).filter(func.lower(BudgetRule.category) == old_lower).delete(
        synchronize_session=False
    )

    deleted_name = cat.name
    db.delete(cat)
    db.commit()

    return CategoryDeleteResult(
        deleted_id=category_id,
        deleted_name=deleted_name,
        reassigned_to=target_name,
        transactions_reassigned=tx_updated,
        rules_reassigned=rule_updated,
        receipt_items_reassigned=item_updated,
        products_reassigned=prod_updated,
        children_promoted=children_promoted,
    )
