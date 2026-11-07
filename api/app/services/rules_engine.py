"""Outlook-style transaction categorization rules engine."""

from typing import Optional, Tuple
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CategorizationRule, Transaction


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

    for tx in transactions:
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
            if changed:
                updated_count += 1

    for rule in rules:
        rule.match_count = rule_match_counts.get(rule.id, 0)

    db.commit()
    return {"evaluated": len(transactions), "updated": updated_count}
