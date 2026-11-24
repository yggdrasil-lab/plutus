"""Tag normalization, parsing, redundancy stripping, and vocabulary collection."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
import json
import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CategorizationRule, ReceiptItem, Transaction


def normalize_tags(tags: Iterable[str], max_length: int = 40) -> list[str]:
    """Normalize tags to deduplicated lowercase kebab-case tokens."""
    seen: list[str] = []
    for raw in tags:
        cleaned = re.sub(r"\s+", "-", str(raw).strip().lower().lstrip("#"))
        if cleaned:
            cleaned = cleaned[:max_length]
            if cleaned not in seen:
                seen.append(cleaned)
    return seen


def strip_redundant_tags(tags: Iterable[str], category: Optional[str]) -> list[str]:
    """Normalize tags and strip 'general' plus any tag matching the category name."""
    cat_lower = (category or "").strip().lower()
    excluded = {"general"}
    if cat_lower:
        excluded.add(cat_lower)
        excluded.add(re.sub(r"\s+", "-", cat_lower))
    return [tag for tag in normalize_tags(tags) if tag not in excluded]


def parse_rule_tags(raw_tags: Optional[str]) -> list[str]:
    """Parse JSON-encoded or comma-separated tag list from a rule, transaction, or item."""
    if not raw_tags:
        return []
    try:
        parsed = json.loads(raw_tags)
        if isinstance(parsed, list):
            return normalize_tags(str(t) for t in parsed)
    except (ValueError, TypeError):
        return normalize_tags(part for part in str(raw_tags).split(",") if part.strip())
    return []


def collect_existing_tags(db: Session, limit: int = 80) -> list[str]:
    """Return existing normalized tags ordered by usage frequency across rules, txs, and items."""
    counts: Counter[str] = Counter()
    for raw in db.scalars(select(Transaction.tags).where(Transaction.tags.is_not(None))).all():
        counts.update(parse_rule_tags(raw))
    for raw in db.scalars(select(ReceiptItem.tags).where(ReceiptItem.tags.is_not(None))).all():
        counts.update(parse_rule_tags(raw))
    for raw in db.scalars(
        select(CategorizationRule.target_tags).where(CategorizationRule.target_tags.is_not(None))
    ).all():
        counts.update(parse_rule_tags(raw))
    counts.pop("general", None)
    return [tag for tag, _ in counts.most_common(limit)]
