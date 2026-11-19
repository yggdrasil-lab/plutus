"""Outlook-style transaction categorization rules engine."""

import json
import re
from typing import Iterable, Optional, Tuple
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CategorizationRule, Transaction


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


def matches_rule(rule: CategorizationRule, raw_description: str, amount: float) -> bool:
    """Evaluates whether a transaction matches an Outlook-style categorization rule."""
    if not rule.is_active or not rule.description_pattern:
        return False

    pattern = rule.description_pattern.strip().lower()
    desc = (raw_description or "").strip().lower()

    # 1. Description condition
    match_type = (rule.match_type or "contains").lower()
    if match_type == "exact":
        desc_matches = desc == pattern
    elif match_type == "starts_with":
        desc_matches = desc.startswith(pattern)
    else:  # default 'contains'
        desc_matches = pattern in desc

    if not desc_matches:
        return False

    # 2. Amount condition (optional)
    if rule.amount_equals is not None:
        target_amt = abs(float(rule.amount_equals))
        tx_amt = abs(float(amount))
        if abs(tx_amt - target_amt) > 0.01:
            return False

    # 3. Income rules only match inflows (positive amounts)
    if (rule.target_category or "").strip().lower() == "income" and float(amount) <= 0:
        return False

    return True


def evaluate_rules(
    raw_description: str,
    amount: float,
    rules: list[CategorizationRule],
) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """Evaluates a transaction against active rules ordered by priority.

    Returns:
        (target_category, rename_merchant, matched_rule_id) or (None, None, None)
    """
    for rule in rules:
        if matches_rule(rule, raw_description, amount):
            return rule.target_category, rule.rename_merchant, rule.id
    return None, None, None


def apply_rules_to_existing_transactions(
    db: Session,
    rule_ids: Optional[list[int]] = None,
) -> dict[str, int]:
    """Retroactively evaluates and applies active rules to stored transactions.

    Returns:
        {"evaluated": int, "updated": int}
    """
    stmt = select(CategorizationRule).where(CategorizationRule.is_active == True)
    if rule_ids:
        stmt = stmt.where(CategorizationRule.id.in_(rule_ids))
    rules = db.scalars(stmt.order_by(CategorizationRule.priority.asc(), CategorizationRule.id.asc())).all()

    if not rules:
        return {"evaluated": 0, "updated": 0}

    transactions = db.scalars(select(Transaction)).all()
    updated_count = 0
    rule_match_counts: dict[int, int] = {r.id: 0 for r in rules}
    rules_by_id: dict[int, CategorizationRule] = {r.id: r for r in rules}

    for tx in transactions:
        # Paired transfers are already reconciled; never clobber them with category rules.
        if tx.linked_transaction_id is not None:
            continue
        desc = tx.raw_description or tx.merchant
        amt = float(tx.amount)
        cat, rename, rule_id = evaluate_rules(desc, amt, rules)
        if cat and rule_id:
            rule_match_counts[rule_id] += 1
            changed = False
            if tx.category != cat:
                tx.category = cat
                changed = True
            if rename and tx.merchant != rename:
                tx.merchant = rename
                changed = True
            matched_rule = rules_by_id.get(rule_id)
            rule_tags = parse_rule_tags(matched_rule.target_tags if matched_rule else None)
            if rule_tags:
                existing_tags = parse_rule_tags(tx.tags)
                merged_tags = normalize_tags([*existing_tags, *rule_tags])
                if merged_tags != existing_tags:
                    tx.tags = json.dumps(merged_tags)
                    changed = True
            if matched_rule and matched_rule.target_recurrence and tx.recurrence != matched_rule.target_recurrence:
                tx.recurrence = matched_rule.target_recurrence
                changed = True
            if changed:
                updated_count += 1

    for rule in rules:
        rule.match_count = rule_match_counts.get(rule.id, 0)

    db.commit()
    return {"evaluated": len(transactions), "updated": updated_count}
