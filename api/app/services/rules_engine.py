"""Outlook-style transaction categorization rules engine."""

import json
import re
from typing import Iterable, Optional, Tuple
from sqlalchemy import func, select
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


def match_receipt_item_rule(
    item_name: str,
    brand: Optional[str],
    total_price: float,
    rules: list[CategorizationRule],
) -> Optional[CategorizationRule]:
    """Return the first active rule matching a receipt line item by name or brand+name."""
    if float(total_price) < 0:
        return None
    amt = -abs(float(total_price))
    candidates = [item_name]
    if brand and brand.strip():
        candidates.append(f"{brand.strip()} {item_name}".strip())
    for rule in rules:
        if any(matches_rule(rule, candidate, amt) for candidate in candidates):
            return rule
    return None


def _apply_rule_to_record(
    record: Transaction | ReceiptItem,
    matched_rule: CategorizationRule,
) -> bool:
    """Apply matched rule category and tags to a Transaction or ReceiptItem."""
    changed = False
    if record.category != matched_rule.target_category:
        record.category = matched_rule.target_category
        changed = True
    rule_tags = parse_rule_tags(matched_rule.target_tags)
    if rule_tags:
        existing_tags = parse_rule_tags(record.tags)
        merged_tags = normalize_tags([*existing_tags, *rule_tags])
        if merged_tags != existing_tags:
            record.tags = json.dumps(merged_tags)
            changed = True
    return changed


def apply_rules_to_existing_transactions(
    db: Session,
    rule_ids: Optional[list[int]] = None,
) -> dict[str, int]:
    """Retroactively evaluates and applies active rules to transactions and receipt items.

    Returns:
        {"evaluated": int, "updated": int, "receipt_items_evaluated": int, "receipt_items_updated": int}
    """
    stmt = select(CategorizationRule).where(CategorizationRule.is_active == True)
    if rule_ids:
        stmt = stmt.where(CategorizationRule.id.in_(rule_ids))
    rules = db.scalars(stmt.order_by(CategorizationRule.priority.asc(), CategorizationRule.id.asc())).all()

    if not rules:
        return {
            "evaluated": 0,
            "updated": 0,
            "receipt_items_evaluated": 0,
            "receipt_items_updated": 0,
        }

    transactions = db.scalars(select(Transaction)).all()
    receipt_items = db.scalars(select(ReceiptItem)).all()
    updated_count = 0
    receipt_items_updated = 0
    rule_match_counts: dict[int, int] = {r.id: 0 for r in rules}
    rules_by_id: dict[int, CategorizationRule] = {r.id: r for r in rules}

    for tx in transactions:
        # Paired transfers are already reconciled; never clobber them with category rules.
        if tx.linked_transaction_id is not None:
            continue
        desc = tx.raw_description or tx.merchant
        amt = float(tx.amount)
        cat, rename, rule_id = evaluate_rules(desc, amt, rules)
        matched_rule = rules_by_id.get(rule_id) if rule_id else None
        if cat and matched_rule:
            rule_match_counts[matched_rule.id] += 1
            changed = _apply_rule_to_record(tx, matched_rule)
            if rename and tx.merchant != rename:
                tx.merchant = rename
                changed = True
            if matched_rule.target_recurrence and tx.recurrence != matched_rule.target_recurrence:
                tx.recurrence = matched_rule.target_recurrence
                changed = True
            if changed:
                updated_count += 1

    for r_item in receipt_items:
        matched_rule = match_receipt_item_rule(
            r_item.item_name,
            r_item.brand,
            float(r_item.total_price),
            rules,
        )
        if matched_rule:
            rule_match_counts[matched_rule.id] += 1
            if _apply_rule_to_record(r_item, matched_rule):
                receipt_items_updated += 1

    for rule in rules:
        rule.match_count = rule_match_counts.get(rule.id, 0)

    db.commit()
    return {
        "evaluated": len(transactions),
        "updated": updated_count,
        "receipt_items_evaluated": len(receipt_items),
        "receipt_items_updated": receipt_items_updated,
    }
