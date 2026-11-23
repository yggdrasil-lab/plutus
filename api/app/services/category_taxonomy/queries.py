"""Read-only seeding, hierarchy lookup, and metrics queries for Category Taxonomy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import func, select, union
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
    CATEGORY_PARENTS,
    DEFAULT_CATEGORY_NAMES,
    DEFAULT_CATEGORY_SPECS,
    _NON_EXPENSE_SYSTEM_LOWER,
    CategoryMetrics,
    CategoryNotFoundError,
    CategorySeedSpec,
    CategorySnapshot,
)


@dataclass(frozen=True)
class _MetricsBundle:
    tx_counts: dict[str, int]
    rule_counts: dict[str, int]
    child_counts: dict[int, int]
    receipt_item_counts: dict[str, int]
    product_counts: dict[str, int]
    budget_rules: dict[str, tuple[str, float, float]]


def _build_seed_row(
    spec: CategorySeedSpec,
    *,
    parent_id: Optional[int],
    annual_budget_cats: set[str],
) -> Category:
    is_annual = not spec.is_system and spec.name.lower() in annual_budget_cats
    return Category(
        name=spec.name,
        parent_id=parent_id,
        is_system=spec.is_system,
        is_active=True,
        amortize_period="annual" if is_annual else None,
        sort_order=spec.sort_order,
    )


def _seed_default_categories(db: Session) -> list[Category]:
    annual_budget_cats = {
        (rule.category or "").strip().lower()
        for rule in db.scalars(select(BudgetRule).where(BudgetRule.is_active == True)).all()
        if (rule.budget_period or "").strip().lower() == "annual"
    }
    created_roots: dict[str, Category] = {}
    for spec in DEFAULT_CATEGORY_SPECS:
        if spec.parent is not None:
            continue
        cat = _build_seed_row(spec, parent_id=None, annual_budget_cats=annual_budget_cats)
        db.add(cat)
        created_roots[spec.name.lower()] = cat
    db.flush()

    for spec in DEFAULT_CATEGORY_SPECS:
        if spec.parent is None:
            continue
        parent_obj = created_roots.get(spec.parent.lower())
        parent_id = parent_obj.id if parent_obj is not None else None
        db.add(_build_seed_row(spec, parent_id=parent_id, annual_budget_cats=annual_budget_cats))

    db.commit()
    return list(
        db.scalars(select(Category).order_by(Category.sort_order.asc(), Category.name.asc())).all()
    )


def ensure_default_categories(db: Session) -> list[Category]:
    """Seed canonical categories only when the categories table is completely empty."""
    existing = list(
        db.scalars(select(Category).order_by(Category.sort_order.asc(), Category.name.asc())).all()
    )
    if existing:
        return existing
    return _seed_default_categories(db)


def get_category_parent_map(db: Optional[Session] = None) -> dict[str, str]:
    """Return active {child_name: parent_name} mapping from DB or default specs."""
    if db is None:
        return dict(CATEGORY_PARENTS)
    rows = db.scalars(select(Category)).all()
    if not rows:
        return dict(CATEGORY_PARENTS)

    by_id = {row.id: row for row in rows}
    parent_map: dict[str, str] = {}
    for row in rows:
        if not row.is_active or row.parent_id is None:
            continue
        parent = by_id.get(row.parent_id)
        if parent and parent.is_active:
            parent_map[row.name] = parent.name
    return parent_map


def get_canonical_category_names(
    db: Optional[Session] = None,
    *,
    include_system_non_expense: bool = True,
) -> list[str]:
    """Return active category names from DB (or default specs when unseeded)."""
    if db is not None and db.scalar(select(Category.id).limit(1)) is not None:
        rows = db.scalars(
            select(Category)
            .where(Category.is_active == True)
            .order_by(Category.sort_order.asc(), Category.name.asc())
        ).all()
        names = [row.name for row in rows]
    else:
        names = list(DEFAULT_CATEGORY_NAMES)

    if include_system_non_expense:
        return names
    return [name for name in names if name.strip().lower() not in _NON_EXPENSE_SYSTEM_LOWER]


def _build_lookup_tables(
    parent_map: Optional[dict[str, str]] = None,
) -> tuple[dict[str, str], dict[str, str]]:
    active = CATEGORY_PARENTS if parent_map is None else parent_map
    lower_to_canonical: dict[str, str] = {name.lower(): name for name in DEFAULT_CATEGORY_NAMES}
    for child, parent in active.items():
        lower_to_canonical[child.lower()] = child
        lower_to_canonical[parent.lower()] = parent
    lower_to_parent: dict[str, str] = {k.lower(): v for k, v in active.items()}
    return lower_to_canonical, lower_to_parent


def get_parent_category(
    category: str,
    parent_map: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """Return the immediate base class for a category, or None if it is a root class."""
    key = (category or "").strip().lower()
    if not key:
        return None
    _, lower_to_parent = _build_lookup_tables(parent_map)
    return lower_to_parent.get(key)


def get_root_category(
    category: str,
    parent_map: Optional[dict[str, str]] = None,
) -> str:
    """Resolve the top-level base class by walking inheritance links safely."""
    current = (category or "General").strip() or "General"
    lower_to_canonical, lower_to_parent = _build_lookup_tables(parent_map)
    seen: set[str] = set()
    while True:
        key = current.lower()
        if key in seen:
            return lower_to_canonical.get(key, current)
        seen.add(key)
        parent = lower_to_parent.get(key)
        if not parent:
            return lower_to_canonical.get(key, current)
        current = parent


def get_descendant_category_names(
    root_category: str,
    parent_map: Optional[dict[str, str]] = None,
) -> set[str]:
    """Return lowercase names for root_category and all descendants at any depth."""
    target_lower = (root_category or "").strip().lower()
    if not target_lower:
        return set()
    active = CATEGORY_PARENTS if parent_map is None else parent_map
    _, lower_to_parent = _build_lookup_tables(active)
    descendants: set[str] = {target_lower}
    for child in active:
        curr = child.strip().lower()
        seen: set[str] = set()
        while curr and curr not in seen:
            if curr == target_lower:
                descendants.add(child.strip().lower())
                break
            seen.add(curr)
            parent = lower_to_parent.get(curr)
            curr = parent.strip().lower() if parent else ""
    return descendants


def _load_distinct_transaction_counts(db: Session) -> dict[str, int]:
    parent_pairs = select(
        func.lower(func.trim(Transaction.category)).label("cat_key"),
        Transaction.id.label("tx_id"),
    )
    split_pairs = select(
        func.lower(func.trim(TransactionSplit.category)).label("cat_key"),
        TransactionSplit.transaction_id.label("tx_id"),
    )
    combined = union(parent_pairs, split_pairs).subquery()
    rows = db.execute(
        select(combined.c.cat_key, func.count(combined.c.tx_id)).group_by(combined.c.cat_key)
    ).all()
    return {str(key): int(cnt) for key, cnt in rows if key}


def _count_by_normalized_name(db: Session, column: Any, pk_column: Any) -> dict[str, int]:
    key_expr = func.lower(func.trim(column))
    rows = db.execute(select(key_expr, func.count(pk_column)).group_by(key_expr)).all()
    return {str(key): int(cnt) for key, cnt in rows if key}


def _load_active_child_counts(db: Session) -> dict[int, int]:
    rows = db.execute(
        select(Category.parent_id, func.count(Category.id))
        .where(Category.parent_id.is_not(None), Category.is_active == True)
        .group_by(Category.parent_id)
    ).all()
    return {int(parent_id): int(cnt) for parent_id, cnt in rows if parent_id is not None}


def _load_active_budget_rules(db: Session) -> dict[str, tuple[str, float, float]]:
    rules = db.scalars(select(BudgetRule).where(BudgetRule.is_active == True)).all()
    return {
        (rule.category or "").strip().lower(): (
            (rule.budget_period or "monthly").strip().lower(),
            float(rule.weekly_limit),
            float(rule.monthly_limit),
        )
        for rule in rules
        if rule.category
    }


def _load_metrics_bundle(db: Session) -> _MetricsBundle:
    return _MetricsBundle(
        tx_counts=_load_distinct_transaction_counts(db),
        rule_counts=_count_by_normalized_name(
            db, CategorizationRule.target_category, CategorizationRule.id
        ),
        child_counts=_load_active_child_counts(db),
        receipt_item_counts=_count_by_normalized_name(db, ReceiptItem.category, ReceiptItem.id),
        product_counts=_count_by_normalized_name(db, Product.category, Product.id),
        budget_rules=_load_active_budget_rules(db),
    )


def _hierarchy_sort_key(cat: Category, by_id: dict[int, Category]) -> tuple[object, ...]:
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


def _build_snapshot(
    cat: Category,
    *,
    by_id: dict[int, Category],
    bundle: _MetricsBundle,
) -> CategorySnapshot:
    key = cat.name.strip().lower()
    parent = by_id.get(cat.parent_id) if cat.parent_id is not None else None
    rule = bundle.budget_rules.get(key)
    weekly = rule[1] if rule is not None else None
    monthly = rule[2] if rule is not None else None
    annual = round(weekly * 52.0, 2) if weekly is not None else None
    resolved_period = (
        rule[0] if rule is not None else ("annual" if cat.amortize_period == "annual" else None)
    )
    metrics = CategoryMetrics(
        transaction_count=bundle.tx_counts.get(key, 0),
        rule_count=bundle.rule_counts.get(key, 0),
        child_count=bundle.child_counts.get(cat.id, 0),
        receipt_item_count=bundle.receipt_item_counts.get(key, 0),
        product_count=bundle.product_counts.get(key, 0),
    )
    return CategorySnapshot(
        id=cat.id,
        name=cat.name,
        parent_id=cat.parent_id,
        parent_name=parent.name if parent is not None else None,
        is_system=bool(cat.is_system),
        is_active=bool(cat.is_active),
        amortize_period=cat.amortize_period,
        sort_order=int(cat.sort_order),
        has_budget_rule=rule is not None,
        budget_period=resolved_period,
        weekly_limit=weekly,
        monthly_limit=monthly,
        annual_limit=annual,
        metrics=metrics,
    )


def list_categories(
    db: Session, *, include_inactive: bool = False
) -> tuple[CategorySnapshot, ...]:
    """List hierarchical category snapshots ordered by root class then subcategories."""
    all_cats = ensure_default_categories(db)
    by_id = {cat.id: cat for cat in all_cats}
    bundle = _load_metrics_bundle(db)
    visible = [cat for cat in all_cats if include_inactive or cat.is_active]
    visible.sort(key=lambda cat: _hierarchy_sort_key(cat, by_id))
    return tuple(_build_snapshot(cat, by_id=by_id, bundle=bundle) for cat in visible)


def get_category_snapshot(db: Session, category_id: int) -> CategorySnapshot:
    """Return a detached CategorySnapshot by primary key or raise CategoryNotFoundError."""
    all_cats = db.scalars(select(Category)).all()
    by_id = {cat.id: cat for cat in all_cats}
    cat = by_id.get(category_id)
    if cat is None:
        raise CategoryNotFoundError("Category not found.")
    bundle = _load_metrics_bundle(db)
    return _build_snapshot(cat, by_id=by_id, bundle=bundle)
