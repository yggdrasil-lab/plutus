"""Shared ingestion, deduplication, and balance snapshot helpers for all connectors."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, AssetSnapshot, CategorizationRule, Transaction
from app.services.jev_client import classify_transaction_jev
from app.services.receipt_service import reconcile_unlinked_receipts
from app.services.rules_engine import evaluate_rules
from app.services.statement_parser import auto_categorize, generate_transaction_hash
from app.services.transfer_service import reconcile_transfers


@dataclass
class NormalizedTx:
    """Provider-agnostic normalized transaction payload."""

    transaction_date: date
    amount: float
    currency: str
    merchant: str
    category: str = "General"
    raw_description: Optional[str] = None
    exchange_rate: Optional[float] = None
    fee_amount: Optional[float] = None
    notes: Optional[str] = None


def get_or_create_external_account(
    db: Session,
    *,
    provider: str,
    external_id: str,
    name: str,
    institution: str,
    account_type: str = "checking",
    tier: str = "cash",
    currency: str = "NZD",
    expected_annual_return: float = 0.0,
) -> Account:
    """Find an Account by (external_provider, external_account_id) or create one."""
    account = db.scalar(
        select(Account).where(
            Account.external_provider == provider,
            Account.external_account_id == external_id,
        )
    )
    if account:
        return account

    account = Account(
        name=name[:100],
        account_type=account_type,
        tier=tier,
        institution=institution[:100],
        currency=currency[:3].upper(),
        expected_annual_return=expected_annual_return,
        is_active=True,
        external_provider=provider,
        external_account_id=external_id,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def ingest_normalized_transactions(
    db: Session,
    account: Account,
    items: list[NormalizedTx],
) -> tuple[int, int, list[Transaction]]:
    """Deduplicate, categorize, persist, and reconcile normalized transactions."""
    if not items:
        return 0, 0, []

    active_rules = db.scalars(
        select(CategorizationRule)
        .where(CategorizationRule.is_active == True)
        .order_by(CategorizationRule.priority.asc(), CategorizationRule.id.asc())
    ).all()

    created: list[Transaction] = []
    skipped = 0

    for item in items:
        raw_hash = generate_transaction_hash(
            tx_date=item.transaction_date,
            amount=item.amount,
            merchant=item.merchant,
            account_id=account.id,
        )
        existing = db.scalar(select(Transaction).where(Transaction.raw_hash == raw_hash))
        if existing:
            skipped += 1
            continue

        desc = item.raw_description or item.merchant
        rule_cat, rename, rule_id = evaluate_rules(desc, item.amount, active_rules)
        merchant_name = (rename if rename else item.merchant)[:150]

        if rule_cat:
            tx_cat = rule_cat
            if rule_id:
                matched_rule = next((r for r in active_rules if r.id == rule_id), None)
                if matched_rule:
                    matched_rule.match_count += 1
        else:
            tx_cat = item.category
            if not tx_cat or tx_cat == "General":
                jev_cat, _ = classify_transaction_jev(merchant_name, item.amount, item.notes)
                tx_cat = jev_cat if jev_cat and jev_cat != "General" else auto_categorize(merchant_name)

        tx = Transaction(
            transaction_date=item.transaction_date,
            account_id=account.id,
            amount=item.amount,
            currency=(item.currency or account.currency or "NZD")[:3].upper(),
            merchant=merchant_name,
            category=tx_cat[:50],
            raw_description=item.raw_description,
            raw_hash=raw_hash,
            exchange_rate=item.exchange_rate,
            fee_amount=item.fee_amount,
            notes=item.notes,
        )
        db.add(tx)
        created.append(tx)

    account.last_synced_at = datetime.now(timezone.utc)
    db.commit()
    for tx in created:
        db.refresh(tx)

    if created:
        reconcile_unlinked_receipts(db)
        reconcile_transfers(db)

    return len(created), skipped, created


def upsert_account_snapshot(
    db: Session,
    *,
    account: Account,
    snapshot_date: date,
    balance: float,
    currency: str,
    source: str,
    notes: Optional[str] = None,
) -> AssetSnapshot:
    """Insert or update the balance snapshot for an account on a given date."""
    existing = db.scalar(
        select(AssetSnapshot).where(
            AssetSnapshot.account_id == account.id,
            AssetSnapshot.snapshot_date == snapshot_date,
        )
    )
    if existing:
        existing.balance = balance
        existing.balance_nzd = balance
        existing.currency = currency[:3].upper()
        existing.source = source[:30]
        if notes:
            existing.notes = notes
        db.commit()
        db.refresh(existing)
        return existing

    snap = AssetSnapshot(
        account_id=account.id,
        snapshot_date=snapshot_date,
        balance=balance,
        currency=currency[:3].upper(),
        exchange_rate=1.0,
        balance_nzd=balance,
        source=source[:30],
        notes=notes,
    )
    db.add(snap)
    db.commit()
    db.refresh(snap)
    return snap
