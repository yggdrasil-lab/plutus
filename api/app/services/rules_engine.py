"""Outlook-style transaction categorization rules engine."""

from collections import Counter
import hashlib
import json
import re
from typing import Any, Iterable, Optional, Tuple
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.models import Account, CategorizationRule, ReceiptItem, Transaction
from app.services.fx_service import get_exchange_rate
from app.services.jev_client import (
    classify_transaction_jev_with_tags,
    get_extra_jev_categories,
    is_jev_circuit_open,
)

CRYPTO_CURRENCIES = {"BTC", "ETH", "SOL", "USDC", "USDT"}
TRANSFER_RULE_CATEGORIES = ("transfers", "transfer")


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

    tx_val = float(amount)
    tx_amt = abs(tx_val)

    # 2. Amount conditions (optional)
    if rule.amount_equals is not None:
        target_amt = abs(float(rule.amount_equals))
        if abs(tx_amt - target_amt) > 0.01:
            return False

    if rule.min_amount is not None and tx_amt < float(rule.min_amount):
        return False

    if rule.max_amount is not None and tx_amt > float(rule.max_amount):
        return False

    # 3. Direction condition (optional)
    rule_dir = (rule.direction or "").strip().lower()
    if rule_dir == "outflow" and tx_val >= 0:
        return False
    if rule_dir == "inflow" and tx_val <= 0:
        return False

    # 4. Income rules only match inflows (positive amounts)
    if (rule.target_category or "").strip().lower() == "income" and tx_val <= 0:
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
    if (
        isinstance(record, Transaction)
        and matched_rule.loan_id is not None
        and record.loan_id != matched_rule.loan_id
    ):
        record.loan_id = matched_rule.loan_id
        record.loan_payment_amount = None
        changed = True
    return changed


def apply_counterpart_account_for_rule(
    db: Session,
    tx: Transaction,
    rule: CategorizationRule,
) -> bool:
    """Apply rule counterpart_account_id attribution or auto-mirroring for Transfers."""
    if rule.counterpart_account_id is None or tx.account_id == rule.counterpart_account_id:
        return False

    counterpart_acc = db.get(Account, rule.counterpart_account_id)
    if counterpart_acc is None:
        return False

    changed = False
    if tx.counterpart_account_id != rule.counterpart_account_id:
        tx.counterpart_account_id = rule.counterpart_account_id
        changed = True

    is_transfer_rule = (rule.target_category or "").strip().lower() in TRANSFER_RULE_CATEGORIES
    if not is_transfer_rule:
        return changed

    if tx.linked_transaction_id is not None:
        return changed

    tx_amt = float(tx.amount)
    if tx_amt == 0.0:
        return changed

    mirror_hash = hashlib.sha256(
        f"mirror:{tx.raw_hash}:{rule.counterpart_account_id}".encode("utf-8")
    ).hexdigest()
    existing_mirror = db.scalar(
        select(Transaction).where(Transaction.raw_hash == mirror_hash)
    )
    if existing_mirror is not None:
        if existing_mirror.linked_transaction_id is None:
            tx.linked_transaction_id = existing_mirror.id
            existing_mirror.linked_transaction_id = tx.id
            existing_mirror.category = "Transfers"
            if existing_mirror.counterpart_account_id != tx.account_id:
                existing_mirror.counterpart_account_id = tx.account_id
            if (tx.currency or "").strip().upper() == (existing_mirror.currency or "").strip().upper():
                tx.exchange_rate = 1.0
                existing_mirror.exchange_rate = 1.0
            return True
        return changed

    if tx.id is None:
        db.flush()

    src_currency = (tx.currency or "NZD").strip().upper()
    dst_currency = (counterpart_acc.currency or src_currency).strip().upper()

    if src_currency == dst_currency:
        mirror_currency = src_currency[:3]
        mirror_amount = -tx_amt
        tx.exchange_rate = 1.0
        mirror_fx: Optional[float] = 1.0
    else:
        resolved = get_exchange_rate(
            db,
            rate_date=tx.transaction_date,
            from_currency=src_currency,
            to_currency=dst_currency,
        )
        if resolved is not None and resolved[0] > 0:
            conv_rate = float(resolved[0])
            mirror_currency = dst_currency[:3]
            decimals = 8 if dst_currency in CRYPTO_CURRENCIES else 2
            mirror_amount = round(-tx_amt * conv_rate, decimals)
            precision = 8 if conv_rate < 0.01 else 6 if conv_rate < 1.0 else 4
            if tx_amt < 0:
                tx.exchange_rate = round(conv_rate, precision)
                if dst_currency == "NZD":
                    mirror_fx = 1.0
                else:
                    dst_nzd = get_exchange_rate(
                        db,
                        rate_date=tx.transaction_date,
                        from_currency=dst_currency,
                        to_currency="NZD",
                    )
                    mirror_fx = (
                        round(float(dst_nzd[0]), 8)
                        if dst_nzd and dst_nzd[0] > 0
                        else None
                    )
            else:
                inv_rate = 1.0 / conv_rate
                inv_precision = 8 if inv_rate < 0.01 else 6 if inv_rate < 1.0 else 4
                mirror_fx = round(inv_rate, inv_precision)
                if src_currency == "NZD":
                    tx.exchange_rate = 1.0
                elif tx.exchange_rate is None:
                    src_nzd = get_exchange_rate(
                        db,
                        rate_date=tx.transaction_date,
                        from_currency=src_currency,
                        to_currency="NZD",
                    )
                    if src_nzd and src_nzd[0] > 0:
                        tx.exchange_rate = round(float(src_nzd[0]), 8)
        else:
            mirror_currency = src_currency[:3]
            mirror_amount = -tx_amt
            tx.exchange_rate = 1.0
            mirror_fx = 1.0

    mirror_tx = Transaction(
        transaction_date=tx.transaction_date,
        account_id=rule.counterpart_account_id,
        counterpart_account_id=tx.account_id,
        amount=mirror_amount,
        currency=mirror_currency,
        merchant=tx.merchant,
        category="Transfers",
        raw_description=tx.raw_description,
        raw_hash=mirror_hash,
        exchange_rate=mirror_fx,
        fee_amount=None,
        tags=tx.tags or "[]",
        recurrence=tx.recurrence,
        notes=f"mirror:{tx.id}",
    )
    db.add(mirror_tx)
    db.flush()
    tx.linked_transaction_id = mirror_tx.id
    mirror_tx.linked_transaction_id = tx.id
    return True


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

    transactions = db.scalars(
        select(Transaction).options(joinedload(Transaction.splits))
    ).unique().all()
    receipt_items = db.scalars(select(ReceiptItem)).all()
    updated_count = 0
    receipt_items_updated = 0
    rule_match_counts: dict[int, int] = {r.id: 0 for r in rules}
    rules_by_id: dict[int, CategorizationRule] = {r.id: r for r in rules}

    for tx in transactions:
        # Paired transfers are already reconciled; never clobber them with category rules.
        if tx.linked_transaction_id is not None:
            continue
        # Manual splits take precedence; never overwrite them during retroactive rule runs.
        if tx.splits:
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
            if apply_counterpart_account_for_rule(db, tx, matched_rule):
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


def classify_uncategorized_transactions(
    db: Session,
    *,
    limit: int = 50,
    include_untagged: bool = False,
    dry_run: bool = True,
    only_outflows: bool = False,
) -> dict[str, Any]:
    """Retroactively classify uncategorized or untagged transactions via Jev System One."""
    stmt = select(Transaction).where(
        Transaction.linked_transaction_id.is_(None),
        Transaction.category.notin_(("Transfers", "Income")),
    )
    if only_outflows:
        stmt = stmt.where(Transaction.amount < 0)
    if include_untagged:
        stmt = stmt.where(
            or_(
                Transaction.category == "General",
                Transaction.tags.is_(None),
                Transaction.tags.in_(("", "[]")),
            )
        )
    else:
        stmt = stmt.where(Transaction.category == "General")

    candidates = db.scalars(
        stmt.order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
    ).all()

    rules = list(
        db.scalars(
            select(CategorizationRule)
            .where(CategorizationRule.is_active == True)
            .order_by(CategorizationRule.priority.asc(), CategorizationRule.id.asc())
        ).all()
    )

    eligible: list[Transaction] = []
    for tx in candidates:
        desc = tx.raw_description or tx.merchant
        rule_cat, _, _ = evaluate_rules(desc, float(tx.amount), rules)
        if rule_cat is not None:
            continue
        eligible.append(tx)

    batch = eligible[:limit]
    remaining = max(0, len(eligible) - len(batch))
    if not batch:
        return {
            "dry_run": dry_run,
            "evaluated": 0,
            "updated": 0,
            "skipped_circuit_open": 0,
            "remaining": remaining,
            "changes": [],
        }

    existing_tags = collect_existing_tags(db)
    extra_categories = get_extra_jev_categories(db)
    verdict_cache: dict[tuple[str, float], tuple[str, list[str]]] = {}

    evaluated = 0
    updated = 0
    skipped_circuit_open = 0
    changes: list[dict[str, Any]] = []

    for idx, tx in enumerate(batch):
        amt = float(tx.amount)
        cache_key = ((tx.merchant or "").strip().lower(), round(amt, 2))
        if cache_key in verdict_cache:
            jev_cat, jev_tags = verdict_cache[cache_key]
        else:
            if is_jev_circuit_open():
                skipped_circuit_open = len(batch) - idx
                break
            jev_cat, _, jev_tags = classify_transaction_jev_with_tags(
                tx.merchant,
                amt,
                tx.notes,
                existing_tags=existing_tags,
                extra_categories=extra_categories,
            )
            verdict_cache[cache_key] = (jev_cat, jev_tags)

        evaluated += 1
        old_category = tx.category
        old_tags = parse_rule_tags(tx.tags)

        if old_category == "General":
            candidate_cat = (jev_cat or "General").strip() or "General"
            if candidate_cat.lower() == "income" and amt <= 0:
                new_category = "General"
            else:
                new_category = candidate_cat
        else:
            new_category = old_category

        new_tags = strip_redundant_tags([*old_tags, *(jev_tags or ())], new_category)

        if new_category != old_category or new_tags != old_tags:
            updated += 1
            changes.append(
                {
                    "transaction_id": tx.id,
                    "transaction_date": tx.transaction_date,
                    "merchant": tx.merchant,
                    "amount": amt,
                    "currency": tx.currency or "NZD",
                    "old_category": old_category,
                    "new_category": new_category,
                    "old_tags": old_tags,
                    "new_tags": new_tags,
                }
            )
            if not dry_run:
                tx.category = new_category[:50]
                tx.tags = json.dumps(new_tags)

    if not dry_run and updated > 0:
        db.commit()

    return {
        "dry_run": dry_run,
        "evaluated": evaluated,
        "updated": updated,
        "skipped_circuit_open": skipped_circuit_open,
        "remaining": remaining,
        "changes": changes,
    }
