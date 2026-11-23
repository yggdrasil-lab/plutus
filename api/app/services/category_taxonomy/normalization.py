"""Legacy category normalization and Akahu category/tag mapping."""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import (
    BudgetRule,
    CategorizationRule,
    Product,
    ReceiptItem,
    Transaction,
    TransactionSplit,
)
from app.services.category_taxonomy._types import DEFAULT_CATEGORY_NAMES
from app.services.category_taxonomy.queries import get_canonical_category_names
from app.services.statement_parser import auto_categorize

AKAHU_CATEGORY_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("hair", "salon", "barber", "beauty", "cosmetic", "skincare", "groom"), "Personal Care"),
    (("supplement", "nutrition", "protein"), "Supplements"),
    (("gym", "fitness", "aquatic", "swim", "pilates", "yoga", "badminton", "sport"), "Fitness"),
    (
        (
            "health",
            "medical",
            "pharmac",
            "chemist",
            "dent",
            "doctor",
            "physio",
            "optom",
            "clinic",
            "hospital",
        ),
        "Medical",
    ),
    (("supermarket", "grocer"), "Groceries"),
    (("liquor", "wine", "beer", "alcohol", "bottle shop"), "Alcohol"),
    (
        ("cafe", "restaurant", "dining", "takeaway", "fast food", "bakery", "bar", "pub", "food"),
        "Dining",
    ),
    (("mechanic", "automotive", "vehicle", "car service", "tyre", "dealer"), "Vehicle"),
    (("fuel", "petrol", "service station"), "Fuel"),
    (("transport", "parking", "taxi", "rideshare", "uber", "transit", "toll"), "Transit"),
    (
        (
            "internet",
            "broadband",
            "mobile",
            "phone",
            "telco",
            "electric",
            "power",
            "gas",
            "water",
            "utilit",
        ),
        "Bills & Telco",
    ),
    (("software", "cloud", "saas", "ai "), "Software & AI"),
    (("clothing", "apparel", "fashion", "footwear", "shoes"), "Clothing"),
    (("electronic", "computer", "tech"), "Tech"),
    (("hardware", "home", "appliance", "furniture"), "Household"),
    (("shopping", "retail", "department store"), "Shopping"),
    (("gaming", "video game"), "Gaming"),
    (("entertainment", "leisure", "recreation", "streaming", "hobby", "cinema", "movie"), "Leisure"),
    (("insurance",), "Insurance"),
    (("travel", "airline", "flight", "hotel", "accommodation"), "Travel"),
    (("rent",), "Rent"),
    (("housing", "mortgage", "rates"), "Housing"),
    (("family", "gift"), "Family"),
    (("loan", "debt", "installment", "instalment"), "Debt & Loans"),
    (("fee", "bank charge", "government", "immigration"), "Fees & Admin"),
    (("transfer", "credit card payment"), "Transfers"),
    (("income", "salary", "wage", "interest", "dividend", "refund"), "Income"),
)

AKAHU_TAG_HINTS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("gym", "fitness"), ("gym", "subscription")),
    (("aquatic", "pool", "swim"), ("swimming",)),
    (("pharmac", "chemist"), ("pharmacy",)),
    (("dent",), ("dental",)),
    (("physio",), ("physio",)),
    (("optom",), ("optometry",)),
    (("doctor", "medical", "clinic", "hospital"), ("medical",)),
    (("supermarket", "grocer"), ("supermarket",)),
    (("cafe", "coffee"), ("cafe",)),
    (("fast food", "takeaway"), ("fast-food", "takeaway")),
    (("bakery",), ("bakery",)),
    (("restaurant", "dining"), ("dining",)),
    (("bar", "pub"), ("drinks",)),
    (("fuel", "petrol"), ("fuel",)),
    (("parking",), ("parking",)),
    (("uber", "taxi", "rideshare"), ("rideshare",)),
    (("transit", "transport"), ("transit",)),
    (("electric", "power"), ("electricity",)),
    (("internet", "broadband"), ("internet", "subscription")),
    (("mobile", "phone", "telco"), ("mobile", "subscription")),
    (("gaming",), ("gaming",)),
    (("streaming",), ("streaming", "subscription")),
    (("cinema", "movie"), ("movies",)),
    (("sport",), ("sports",)),
    (("airline", "flight"), ("flights",)),
    (("hotel", "accommodation"), ("accommodation",)),
)


def _concise_akahu_tags(lower_category: str, canonical: str) -> tuple[str, ...]:
    """Derive concise, composable atomic tags from Akahu's verbose category string."""
    if not lower_category or lower_category in ("unknown", "other", "general", canonical.lower()):
        return ()
    for keywords, tags in AKAHU_TAG_HINTS:
        if any(kw in lower_category for kw in keywords):
            return tags
    return ()


def normalize_akahu_category(
    raw_category: Optional[str],
    merchant: str,
    raw_description: str,
) -> tuple[str, tuple[str, ...]]:
    """Map Akahu's raw category name into Plutus canonical categories plus concise tags."""
    cleaned = (raw_category or "").strip()
    lower = cleaned.lower()

    canonical = ""
    if lower and lower not in ("unknown", "other", "general"):
        for keywords, target in AKAHU_CATEGORY_RULES:
            if any(kw in lower for kw in keywords):
                canonical = target
                break

    if not canonical:
        canonical = auto_categorize(f"{merchant} {raw_description}".strip())

    return canonical, _concise_akahu_tags(lower, canonical)


def _resolve_legacy_target(
    raw_category: Optional[str],
    merchant: str,
    raw_description: str,
    canonical_by_lower: dict[str, str],
) -> tuple[str, tuple[str, ...]]:
    cleaned = (raw_category or "").strip()
    existing_canonical = canonical_by_lower.get(cleaned.lower())
    if existing_canonical is not None:
        return existing_canonical, ()
    canonical, new_tags = normalize_akahu_category(raw_category, merchant, raw_description)
    return canonical_by_lower.get(canonical.lower(), canonical), new_tags


def _merge_tags_json(raw_tags: Optional[str], new_tags: tuple[str, ...]) -> tuple[str, bool]:
    try:
        existing = json.loads(raw_tags) if raw_tags else []
        if not isinstance(existing, list):
            existing = []
    except (ValueError, TypeError):
        existing = []
    merged = list(existing)
    for tag in new_tags:
        if tag not in merged:
            merged.append(tag)
    serialized = json.dumps(merged)
    return serialized, serialized != (raw_tags or "[]") or len(merged) != len(existing)


def _normalize_transactions(
    db: Session,
    canonical_names: set[str],
    canonical_by_lower: dict[str, str],
) -> int:
    candidates = list(
        db.scalars(select(Transaction).where(Transaction.category.notin_(canonical_names))).all()
    )
    updated = 0
    for tx in candidates:
        canonical, new_tags = _resolve_legacy_target(
            tx.category, tx.merchant or "", tx.raw_description or "", canonical_by_lower
        )
        merged_json, tags_changed = _merge_tags_json(tx.tags, new_tags)
        if tx.category != canonical or tags_changed:
            tx.category = canonical
            tx.tags = merged_json
            updated += 1
    return updated


def _normalize_splits(
    db: Session,
    canonical_names: set[str],
    canonical_by_lower: dict[str, str],
) -> int:
    candidates = list(
        db.scalars(
            select(TransactionSplit)
            .options(joinedload(TransactionSplit.transaction))
            .where(TransactionSplit.category.notin_(canonical_names))
        ).all()
    )
    updated = 0
    for split in candidates:
        parent = split.transaction
        merchant = parent.merchant or "" if parent is not None else ""
        raw_desc = parent.raw_description or "" if parent is not None else ""
        canonical, _ = _resolve_legacy_target(
            split.category, merchant, raw_desc, canonical_by_lower
        )
        if split.category != canonical:
            split.category = canonical
            updated += 1
    return updated


def _normalize_simple_table(
    db: Session,
    model: type[Any],
    column: Any,
    cat_attr: str,
    hint_attr: str,
    canonical_names: set[str],
    canonical_by_lower: dict[str, str],
) -> int:
    candidates = list(db.scalars(select(model).where(column.notin_(canonical_names))).all())
    updated = 0
    for row in candidates:
        raw_cat = getattr(row, cat_attr)
        hint = getattr(row, hint_attr) or ""
        canonical, _ = _resolve_legacy_target(raw_cat, hint, "", canonical_by_lower)
        if raw_cat != canonical:
            setattr(row, cat_attr, canonical)
            updated += 1
    return updated


def _partition_budget_rules(
    db: Session, canonical_names: set[str]
) -> tuple[dict[str, BudgetRule], list[BudgetRule]]:
    all_rules = list(
        db.scalars(
            select(BudgetRule).order_by(BudgetRule.is_active.desc(), BudgetRule.id.asc())
        ).all()
    )
    seen_by_lower: dict[str, BudgetRule] = {}
    non_canonical: list[BudgetRule] = []
    for rule in all_rules:
        key = (rule.category or "").strip().lower()
        if rule.category in canonical_names and key not in seen_by_lower:
            seen_by_lower[key] = rule
        else:
            non_canonical.append(rule)
    return seen_by_lower, non_canonical


def _normalize_budget_rules(
    db: Session,
    canonical_names: set[str],
    canonical_by_lower: dict[str, str],
) -> int:
    seen_by_lower, non_canonical = _partition_budget_rules(db, canonical_names)
    to_delete: list[BudgetRule] = []
    pending_updates: dict[int, tuple[BudgetRule, str]] = {}

    for rule in non_canonical:
        canonical, _ = _resolve_legacy_target(rule.category, "", "", canonical_by_lower)
        target_lower = canonical.lower()
        if target_lower in seen_by_lower:
            retained = seen_by_lower[target_lower]
            current_name = (
                pending_updates[retained.id][1]
                if retained.id in pending_updates
                else retained.category
            )
            if current_name != canonical:
                pending_updates[retained.id] = (retained, canonical)
            to_delete.append(rule)
            continue
        seen_by_lower[target_lower] = rule
        if rule.category != canonical:
            pending_updates[rule.id] = (rule, canonical)

    for dup in to_delete:
        db.delete(dup)
    if to_delete:
        db.flush()
    for rule, canonical in pending_updates.values():
        rule.category = canonical
    if pending_updates:
        db.flush()
    return len(to_delete) + len(pending_updates)


def normalize_existing_legacy_categories(db: Session) -> int:
    """Normalize non-canonical categories across all six denormalized ledger tables."""
    canonical_names = set(get_canonical_category_names(db)) or set(DEFAULT_CATEGORY_NAMES)
    canonical_by_lower = {name.lower(): name for name in canonical_names}

    updated = 0
    updated += _normalize_transactions(db, canonical_names, canonical_by_lower)
    updated += _normalize_splits(db, canonical_names, canonical_by_lower)
    updated += _normalize_simple_table(
        db,
        CategorizationRule,
        CategorizationRule.target_category,
        "target_category",
        "description_pattern",
        canonical_names,
        canonical_by_lower,
    )
    updated += _normalize_simple_table(
        db,
        ReceiptItem,
        ReceiptItem.category,
        "category",
        "item_name",
        canonical_names,
        canonical_by_lower,
    )
    updated += _normalize_simple_table(
        db,
        Product,
        Product.category,
        "category",
        "name",
        canonical_names,
        canonical_by_lower,
    )
    updated += _normalize_budget_rules(db, canonical_names, canonical_by_lower)
    if updated > 0:
        db.commit()
    return updated
