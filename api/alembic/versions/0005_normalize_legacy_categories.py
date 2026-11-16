"""Normalize legacy Akahu/sub-category names into canonical categories + tags.

Revision ID: 0005_normalize_legacy_categories
Revises: 0004_add_exchange_rates
Create Date: 2026-11-16 14:07:00.000000

"""
import json
import re
from typing import Optional, Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0005_normalize_legacy_categories"
down_revision: Union[str, None] = "0004_add_exchange_rates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CANONICAL_CATEGORIES: frozenset[str] = frozenset(
    {
        "Groceries",
        "Food",
        "Transport",
        "Utilities",
        "Health",
        "Leisure",
        "Insurance",
        "Travel",
        "Housing",
        "Transfers",
        "Income",
        "General",
    }
)

LEGACY_CATEGORY_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("gym", "fitness", "health", "medical", "pharmacy", "chemist", "dental", "doctor", "physio", "optom"), "Health"),
    (("supermarket", "grocer"), "Groceries"),
    (("cafe", "coffee", "restaurant", "dining", "takeaway", "fast food", "bakery", "bar", "pub", "food"), "Food"),
    (("transport", "fuel", "petrol", "parking", "taxi", "rideshare", "uber", "transit", "toll", "vehicle"), "Transport"),
    (("utilit", "electric", "power", "gas", "water", "internet", "broadband", "mobile", "phone", "telco"), "Utilities"),
    (("entertainment", "leisure", "recreation", "gaming", "streaming", "sport", "hobby", "cinema", "movie"), "Leisure"),
    (("insurance",), "Insurance"),
    (("travel", "airline", "flight", "hotel", "accommodation"), "Travel"),
    (("housing", "rent", "mortgage", "rates", "hardware", "home"), "Housing"),
    (("transfer", "credit card payment"), "Transfers"),
    (("income", "salary", "wage", "interest", "dividend", "refund"), "Income"),
)


def _map_legacy_category(raw_category: Optional[str]) -> tuple[str, Optional[str]]:
    cleaned = (raw_category or "").strip()
    if not cleaned:
        return "General", None

    # Exact case-insensitive match to canonical
    for canonical in CANONICAL_CATEGORIES:
        if cleaned.lower() == canonical.lower():
            return canonical, None

    lower = cleaned.lower()
    target = "General"
    for keywords, mapped in LEGACY_CATEGORY_RULES:
        if any(kw in lower for kw in keywords):
            target = mapped
            break

    slug = re.sub(r"[^a-z0-9]+", "-", lower).strip("-")[:40]
    if not slug or slug in ("unknown", "other", "general", target.lower()):
        return target, None
    return target, slug


def upgrade() -> None:
    bind = op.get_bind()

    tx_rows = bind.execute(
        sa.text("SELECT id, category, tags FROM transactions")
    ).fetchall()
    for tx_id, raw_category, raw_tags in tx_rows:
        if raw_category in CANONICAL_CATEGORIES:
            continue
        canonical, slug = _map_legacy_category(raw_category)
        try:
            tags = json.loads(raw_tags) if raw_tags else []
            if not isinstance(tags, list):
                tags = []
        except (ValueError, TypeError):
            tags = []

        if slug and slug not in tags:
            tags.append(slug)

        bind.execute(
            sa.text("UPDATE transactions SET category = :cat, tags = :tags WHERE id = :id"),
            {"cat": canonical, "tags": json.dumps(tags), "id": tx_id},
        )

    rule_rows = bind.execute(
        sa.text("SELECT id, category FROM categorization_rules")
    ).fetchall()
    for rule_id, raw_category in rule_rows:
        if raw_category in CANONICAL_CATEGORIES:
            continue
        canonical, _ = _map_legacy_category(raw_category)
        bind.execute(
            sa.text("UPDATE categorization_rules SET category = :cat WHERE id = :id"),
            {"cat": canonical, "id": rule_id},
        )

    budget_rows = bind.execute(
        sa.text("SELECT id, category FROM budget_rules")
    ).fetchall()
    existing_budget_cats = {row[1] for row in budget_rows}
    for budget_id, raw_category in budget_rows:
        if raw_category in CANONICAL_CATEGORIES:
            continue
        canonical, _ = _map_legacy_category(raw_category)
        if canonical in existing_budget_cats:
            continue
        existing_budget_cats.add(canonical)
        bind.execute(
            sa.text("UPDATE budget_rules SET category = :cat WHERE id = :id"),
            {"cat": canonical, "id": budget_id},
        )


def downgrade() -> None:
    # Data normalization is intentionally non-reversible.
    pass
