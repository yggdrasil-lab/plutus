"""Unified transaction write seam with rules, Jev fallback, FX lookup, dedup, and reconciliation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import json
from typing import Optional
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, CategorizationRule, Transaction
from app.services.ai_gateway import AiGateway, get_default_ai_gateway
from app.services.fx_service import FxRateFetcher, resolve_reporting_rate
from app.services.jev_client import (
    classify_transaction_jev_with_tags,
    get_extra_jev_categories,
)
from app.services.ledger_entry._types import (
    MANUAL_WRITE_POLICY,
    HydratedTransaction,
    LedgerEntryNotFoundError,
    TransactionWriteInput,
    TransactionWritePolicy,
    TransactionWriteResult,
)
from app.services.ledger_entry.hashing import generate_transaction_hash
from app.services.ledger_entry.hydration import load_hydrated_transaction
from app.services.ledger_entry.tags import (
    collect_existing_tags,
    normalize_tags,
    parse_rule_tags,
    strip_redundant_tags,
)
from app.services.receipt_ingestion.matching import reconcile_unlinked_receipts
from app.services.rules_engine import matches_rule
from app.services.statement_parser import auto_categorize
from app.services.transfer_reconciliation import (
    apply_counterpart_account_for_rule,
    reconcile_transfers,
)


@dataclass(frozen=True)
class _ResolvedClassification:
    merchant: str
    category: str
    extra_tags: tuple[str, ...]
    recurrence: Optional[str]
    loan_id: Optional[int]
    notes: Optional[str]
    matched_rule: Optional[CategorizationRule]


def resolve_default_account(
    db: Session,
    *,
    account_name: Optional[str] = None,
) -> Account:
    """Resolve an active account by name or fall back to primary active checking (auto-creating if absent)."""
    cleaned_name = (account_name or "").strip()
    if cleaned_name:
        named_account = db.scalar(
            select(Account).where(
                Account.name.ilike(f"%{cleaned_name}%"),
                Account.is_active == True,
            )
        )
        if named_account is not None:
            return named_account

    checking_account = db.scalar(
        select(Account).where(
            Account.account_type == "checking",
            Account.is_active == True,
        )
    )
    if checking_account is not None:
        return checking_account

    created = Account(
        name="Primary Checking",
        account_type="checking",
        tier="cash",
        institution="Bank",
        currency="NZD",
        expected_annual_return=0.0,
        is_active=True,
    )
    db.add(created)
    db.commit()
    db.refresh(created)
    return created


def _resolve_target_account(db: Session, entry: TransactionWriteInput) -> Account:
    """Resolve explicit account_id (raising LedgerEntryNotFoundError if missing) or default account."""
    if entry.account_id is None:
        return resolve_default_account(db, account_name=entry.account_name)
    account = db.get(Account, entry.account_id)
    if account is None:
        raise LedgerEntryNotFoundError("Account not found")
    return account


def _resolve_hash_or_duplicate(
    db: Session,
    entry: TransactionWriteInput,
    account_id: int,
    *,
    deduplicate: bool,
) -> tuple[str, Optional[HydratedTransaction]]:
    """Compute raw_hash and short-circuit with hydrated existing row when deduplicating."""
    raw_hash = generate_transaction_hash(
        tx_date=entry.transaction_date,
        amount=entry.amount,
        merchant=entry.merchant,
        account_id=account_id,
    )
    existing_id = db.scalar(select(Transaction.id).where(Transaction.raw_hash == raw_hash))
    if existing_id is None:
        return raw_hash, None
    if deduplicate:
        return raw_hash, load_hydrated_transaction(db, existing_id)
    salted_hash = hashlib.sha256(f"{raw_hash}:{uuid4().hex}".encode("utf-8")).hexdigest()
    return salted_hash, None


def _match_active_rule(
    db: Session,
    raw_description: str,
    merchant: str,
    amount: float,
) -> Optional[CategorizationRule]:
    """Match active categorization rules in priority order across raw_description and merchant."""
    rules = db.scalars(
        select(CategorizationRule)
        .where(CategorizationRule.is_active == True)
        .order_by(CategorizationRule.priority.asc(), CategorizationRule.id.asc())
    ).all()
    candidates = list(dict.fromkeys(text for text in (raw_description, merchant) if text))
    for rule in rules:
        if any(matches_rule(rule, text, amount) for text in candidates):
            return rule
    return None


def _classify_via_jev(
    db: Session,
    entry: TransactionWriteInput,
    ai_gateway: AiGateway | None,
) -> tuple[str, list[str], Optional[str]]:
    """Invoke Jev classification and anomaly detection with deterministic heuristic fallback."""
    gateway = ai_gateway or get_default_ai_gateway()
    jev_cat, is_anomaly, jev_tags = classify_transaction_jev_with_tags(
        entry.merchant,
        entry.amount,
        entry.notes,
        existing_tags=collect_existing_tags(db),
        extra_categories=get_extra_jev_categories(db),
        gateway=gateway,
    )
    resolved_cat = (
        jev_cat if jev_cat and jev_cat != "General" else auto_categorize(entry.merchant)
    )
    cleaned_tags = strip_redundant_tags(jev_tags, resolved_cat)
    notes = entry.notes
    if is_anomaly:
        base_notes = (notes or "").strip()
        notes = f"{base_notes} [Anomaly Detected]".strip() if base_notes else "[Anomaly Detected]"
    return resolved_cat[:50], cleaned_tags, notes


def _resolve_write_classification(
    db: Session,
    entry: TransactionWriteInput,
    *,
    raw_description: str,
    policy: TransactionWritePolicy,
    ai_gateway: AiGateway | None,
) -> _ResolvedClassification:
    """Resolve merchant, category, tags, recurrence, loan_id, and notes per policy precedence."""
    if policy.apply_rules:
        matched_rule = _match_active_rule(db, raw_description, entry.merchant, entry.amount)
        if matched_rule is not None:
            matched_rule.match_count = (matched_rule.match_count or 0) + 1
            return _ResolvedClassification(
                merchant=(matched_rule.rename_merchant or entry.merchant)[:150],
                category=matched_rule.target_category[:50],
                extra_tags=tuple(parse_rule_tags(matched_rule.target_tags)),
                recurrence=entry.recurrence or matched_rule.target_recurrence,
                loan_id=matched_rule.loan_id,
                notes=entry.notes,
                matched_rule=matched_rule,
            )

    explicit_category = (entry.category or "").strip()
    if explicit_category and explicit_category.lower() != "general":
        return _ResolvedClassification(
            merchant=entry.merchant[:150],
            category=explicit_category[:50],
            extra_tags=(),
            recurrence=entry.recurrence,
            loan_id=None,
            notes=entry.notes,
            matched_rule=None,
        )

    if policy.use_jev_fallback:
        jev_cat, jev_tags, jev_notes = _classify_via_jev(db, entry, ai_gateway)
        return _ResolvedClassification(
            merchant=entry.merchant[:150],
            category=jev_cat,
            extra_tags=tuple(jev_tags),
            recurrence=entry.recurrence,
            loan_id=None,
            notes=jev_notes,
            matched_rule=None,
        )

    return _ResolvedClassification(
        merchant=entry.merchant[:150],
        category=explicit_category[:50] or "General",
        extra_tags=(),
        recurrence=entry.recurrence,
        loan_id=None,
        notes=entry.notes,
        matched_rule=None,
    )


def _serialize_tags(
    entry_tags: Sequence[str],
    extra_tags: Sequence[str],
    *,
    normalize: bool,
) -> str:
    """Combine caller and derived tags and serialize as a JSON list string."""
    combined = [*(entry_tags or ()), *extra_tags]
    if normalize:
        return json.dumps(normalize_tags(combined))
    cleaned = [str(tag).strip() for tag in combined if str(tag).strip()]
    return json.dumps(cleaned)


def _resolve_reporting_exchange_rate(
    db: Session,
    entry: TransactionWriteInput,
    *,
    tx_currency: str,
    policy: TransactionWritePolicy,
    fx_fetcher: FxRateFetcher | None,
) -> Optional[float]:
    """Use explicit exchange_rate or resolve currency->NZD rate when configured by policy."""
    if entry.exchange_rate is not None:
        return entry.exchange_rate
    if not policy.resolve_reporting_fx:
        return None
    return resolve_reporting_rate(
        db,
        tx_currency,
        entry.transaction_date,
        fetcher=fx_fetcher,
    )


def record_transaction(
    db: Session,
    entry: TransactionWriteInput,
    *,
    policy: TransactionWritePolicy = MANUAL_WRITE_POLICY,
    ai_gateway: AiGateway | None = None,
    fx_fetcher: FxRateFetcher | None = None,
) -> TransactionWriteResult:
    """Persist a transaction with deduplication, rule/Jev classification, FX lookup, and reconciliation."""
    account = _resolve_target_account(db, entry)
    raw_hash, existing_tx = _resolve_hash_or_duplicate(
        db,
        entry,
        account.id,
        deduplicate=policy.deduplicate,
    )
    if existing_tx is not None:
        return TransactionWriteResult(transaction=existing_tx, created=False)

    raw_description = entry.raw_description or entry.merchant
    resolved = _resolve_write_classification(
        db,
        entry,
        raw_description=raw_description,
        policy=policy,
        ai_gateway=ai_gateway,
    )
    tx_currency = (
        (entry.currency or "").strip() or (account.currency or "NZD").strip() or "NZD"
    ).upper()[:3]
    exchange_rate = _resolve_reporting_exchange_rate(
        db,
        entry,
        tx_currency=tx_currency,
        policy=policy,
        fx_fetcher=fx_fetcher,
    )

    tx = Transaction(
        transaction_date=entry.transaction_date,
        account_id=account.id,
        amount=entry.amount,
        currency=tx_currency,
        merchant=resolved.merchant,
        category=resolved.category,
        raw_description=raw_description,
        raw_hash=raw_hash,
        linked_transaction_id=entry.linked_transaction_id,
        loan_id=resolved.loan_id,
        exchange_rate=exchange_rate,
        fee_amount=entry.fee_amount,
        tags=_serialize_tags(entry.tags, resolved.extra_tags, normalize=policy.normalize_tags),
        recurrence=resolved.recurrence,
        notes=resolved.notes,
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    _run_post_write_reconciliation(
        db,
        tx,
        matched_rule=resolved.matched_rule,
        policy=policy,
        fx_fetcher=fx_fetcher,
    )
    return TransactionWriteResult(
        transaction=load_hydrated_transaction(db, tx.id, populate_existing=True),
        created=True,
    )


def _run_post_write_reconciliation(
    db: Session,
    tx: Transaction,
    *,
    matched_rule: Optional[CategorizationRule],
    policy: TransactionWritePolicy,
    fx_fetcher: FxRateFetcher | None,
) -> None:
    """Run post-write receipt/transfer matching and rule counterpart mirroring."""
    if policy.reconcile_post_write:
        reconcile_unlinked_receipts(db)
        reconcile_transfers(db, fetcher=fx_fetcher)
    if (
        policy.apply_counterpart_rules
        and matched_rule is not None
        and matched_rule.counterpart_account_id is not None
    ):
        db.refresh(tx)
        if apply_counterpart_account_for_rule(db, tx, matched_rule, fetcher=fx_fetcher):
            db.commit()
