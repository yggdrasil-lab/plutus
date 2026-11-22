"""Canonical OOP-inspired category inheritance hierarchy for Plutus.

Leaf categories (subclasses) inherit from base categories (superclasses) so
reports can inspect granular spending (`Dining`, `Groceries`, `Alcohol`, `Vehicle`)
or roll up to macro budget pillars (`Food`, `Transport`, `Health`, `Finance`).
Categories are persisted in the `categories` table and fall back cleanly to
default specs when queried against an unseeded test database.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BudgetRule, Category


@dataclass(frozen=True)
class CategorySeedSpec:
    """Default hierarchical category definition."""

    name: str
    parent: Optional[str] = None
    is_system: bool = False
    sort_order: int = 100


DEFAULT_CATEGORY_SPECS: tuple[CategorySeedSpec, ...] = (
    CategorySeedSpec("Food", None, False, 10),
    CategorySeedSpec("Dining", "Food", False, 11),
    CategorySeedSpec("Groceries", "Food", False, 12),
    CategorySeedSpec("Alcohol", "Food", False, 13),
    CategorySeedSpec("Housing", None, False, 20),
    CategorySeedSpec("Rent", "Housing", False, 21),
    CategorySeedSpec("Household", "Housing", False, 22),
    CategorySeedSpec("Transport", None, False, 30),
    CategorySeedSpec("Fuel", "Transport", False, 31),
    CategorySeedSpec("Transit", "Transport", False, 32),
    CategorySeedSpec("Vehicle", "Transport", False, 33),
    CategorySeedSpec("Health", None, False, 40),
    CategorySeedSpec("Fitness", "Health", False, 41),
    CategorySeedSpec("Medical", "Health", False, 42),
    CategorySeedSpec("Supplements", "Health", False, 43),
    CategorySeedSpec("Personal Care", "Health", False, 44),
    CategorySeedSpec("Shopping", None, False, 50),
    CategorySeedSpec("Clothing", "Shopping", False, 51),
    CategorySeedSpec("Tech", "Shopping", False, 52),
    CategorySeedSpec("Utilities", None, False, 60),
    CategorySeedSpec("Bills & Telco", "Utilities", False, 61),
    CategorySeedSpec("Software & AI", "Utilities", False, 62),
    CategorySeedSpec("Insurance", None, False, 70),
    CategorySeedSpec("Leisure", None, False, 80),
    CategorySeedSpec("Gaming", "Leisure", False, 81),
    CategorySeedSpec("Travel", None, False, 90),
    CategorySeedSpec("Family", None, False, 100),
    CategorySeedSpec("Finance", None, False, 110),
    CategorySeedSpec("Fees & Admin", "Finance", False, 111),
    CategorySeedSpec("Debt & Loans", "Finance", True, 112),
    CategorySeedSpec("General", None, True, 900),
    CategorySeedSpec("Transfers", None, True, 910),
    CategorySeedSpec("Income", None, True, 920),
)

CATEGORY_PARENTS: dict[str, str] = {
    spec.name: spec.parent for spec in DEFAULT_CATEGORY_SPECS if spec.parent is not None
}
DEFAULT_CATEGORY_NAMES: tuple[str, ...] = tuple(spec.name for spec in DEFAULT_CATEGORY_SPECS)
SYSTEM_CATEGORY_NAMES: frozenset[str] = frozenset(
    spec.name for spec in DEFAULT_CATEGORY_SPECS if spec.is_system
)
_NON_EXPENSE_SYSTEM_LOWER = frozenset({"transfers", "transfer", "income"})


def ensure_default_categories(db: Session) -> list[Category]:
    """Seed canonical categories only when the categories table is completely empty."""
    existing = list(
        db.scalars(select(Category).order_by(Category.sort_order.asc(), Category.name.asc())).all()
    )
    if existing:
        return existing

    annual_budget_cats = {
        (rule.category or "").strip().lower()
        for rule in db.scalars(select(BudgetRule).where(BudgetRule.is_active == True)).all()
        if (rule.budget_period or "").strip().lower() == "annual"
    }

    created_roots: dict[str, Category] = {}
    for spec in DEFAULT_CATEGORY_SPECS:
        if spec.parent is not None:
            continue
        cat = Category(
            name=spec.name,
            parent_id=None,
            is_system=spec.is_system,
            is_active=True,
            amortize_period="annual" if spec.name.lower() in annual_budget_cats else None,
            sort_order=spec.sort_order,
        )
        db.add(cat)
        created_roots[spec.name.lower()] = cat
    db.flush()

    for spec in DEFAULT_CATEGORY_SPECS:
        if spec.parent is None:
            continue
        parent_obj = created_roots.get(spec.parent.lower())
        cat = Category(
            name=spec.name,
            parent_id=parent_obj.id if parent_obj else None,
            is_system=spec.is_system,
            is_active=True,
            amortize_period="annual" if spec.name.lower() in annual_budget_cats else None,
            sort_order=spec.sort_order,
        )
        db.add(cat)

    db.commit()
    return list(
        db.scalars(select(Category).order_by(Category.sort_order.asc(), Category.name.asc())).all()
    )


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
