"""Canonical OOP-inspired category inheritance hierarchy for Plutus.

Leaf categories (subclasses) inherit from base categories (superclasses) so
reports can inspect granular spending (`Clothing`, `Personal Care`, `Vehicle`)
or roll up to macro budget pillars (`Shopping`, `Health`, `Transport`).
Tags remain orthogonal cross-cutting interfaces across any branch.
"""

from __future__ import annotations

from typing import Optional

CATEGORY_PARENTS: dict[str, str] = {
    "Groceries": "Food",
    "Supplements": "Health",
    "Personal Care": "Health",
    "Vehicle": "Transport",
    "Household": "Housing",
    "Clothing": "Shopping",
    "Tech": "Shopping",
}

_LOWER_TO_CANONICAL: dict[str, str] = {k.lower(): k for k in CATEGORY_PARENTS}
_LOWER_TO_PARENT: dict[str, str] = {k.lower(): v for k, v in CATEGORY_PARENTS.items()}


def get_parent_category(category: str) -> Optional[str]:
    """Return the immediate base class for a category, or None if it is a root class."""
    key = (category or "").strip().lower()
    return _LOWER_TO_PARENT.get(key)


def get_root_category(category: str) -> str:
    """Resolve the top-level base class by walking inheritance links safely."""
    current = (category or "General").strip() or "General"
    seen: set[str] = set()
    while True:
        key = current.lower()
        if key in seen:
            return current
        seen.add(key)
        parent = _LOWER_TO_PARENT.get(key)
        if not parent:
            return _LOWER_TO_CANONICAL.get(key, current)
        current = parent
