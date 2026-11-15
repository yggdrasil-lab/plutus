"""Shared ingestion, deduplication, and balance snapshot helpers for all connectors."""

# ============================================================================
# 1. IMPORTS & CONSTANTS
# ============================================================================
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, AssetSnapshot, CategorizationRule, Transaction
from app.schemas import SyncAccountSummary, SyncResult
from app.services.jev_client import classify_transaction_jev
from app.services.receipt_service import reconcile_unlinked_receipts
from app.services.rules_engine import evaluate_rules
from app.services.statement_parser import auto_categorize, generate_transaction_hash
from app.services.transfer_service import reconcile_transfers


# ============================================================================
# 2. DOMAIN MODELS (Pure @dataclass(frozen=True) - Zero I/O)
# ============================================================================
@dataclass(frozen=True)
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


@dataclass(frozen=True)
class ExternalAccountSpec:
    """Provider-agnostic external account metadata and optional balance."""

    provider: str
    external_id: str
    name: str
    institution: str
    account_type: str = "checking"
    tier: str = "cash"
    currency: str = "NZD"
    current_balance: Optional[float] = None
    snapshot_notes: Optional[str] = None


@dataclass(frozen=True)
class ProviderAccountFeed:
    """Immutable bundle of one external account and its normalized transactions."""

    account: ExternalAccountSpec
    transactions: tuple[NormalizedTx, ...] = ()


# ============================================================================
# 3. GATEWAY PROTOCOL (Dependency Inversion Boundary)
# ============================================================================
class BankFeedGateway(Protocol):
    """Abstract gateway for external bank/wallet feed providers."""

    def fetch_feeds(self, *, days: int) -> list[ProviderAccountFeed]:
        """Fetch normalized account balances and transactions for the lookback window."""
        ...


# ============================================================================
# 4. PERSISTENCE & ORCHESTRATION
# ============================================================================
def get_or_create_external_account(
    db: Session,
    spec: ExternalAccountSpec,
) -> Account:
    """Find an Account by (external_provider, external_account_id) or create one."""
    account = db.scalar(
        select(Account).where(
            Account.external_provider == spec.provider,
            Account.external_account_id == spec.external_id,
        )
    )
    if account:
        return account

    # Link to an existing unlinked manual account when institution and currency match
    unlinked_candidates = db.scalars(
        select(Account).where(
            Account.external_provider.is_(None),
            Account.is_active == True,
            Account.currency == spec.currency[:3].upper(),
            Account.institution.ilike(f"%{spec.institution}%"),
        )
    ).all()
    matched = next(
        (c for c in unlinked_candidates if c.account_type == spec.account_type),
        unlinked_candidates[0] if len(unlinked_candidates) == 1 else None,
    )
    if matched:
        matched.external_provider = spec.provider
        matched.external_account_id = spec.external_id
        db.commit()
        db.refresh(matched)
        return matched

    account = Account(
        name=spec.name[:100],
        account_type=spec.account_type,
        tier=spec.tier,
        institution=spec.institution[:100],
        currency=spec.currency[:3].upper(),
        expected_annual_return=0.0,
        is_active=True,
        external_provider=spec.provider,
        external_account_id=spec.external_id,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def _resolve_category_and_merchant(
    item: NormalizedTx,
    active_rules: list[CategorizationRule],
) -> tuple[str, str]:
    """Resolve final (merchant, category) using rules, Jev, or heuristic fallback."""
    desc = item.raw_description or item.merchant
    rule_cat, rename, rule_id = evaluate_rules(desc, item.amount, active_rules)
    merchant_name = (rename if rename else item.merchant)[:150]

    if rule_cat:
        if rule_id:
            matched = next((r for r in active_rules if r.id == rule_id), None)
            if matched:
                matched.match_count += 1
        return merchant_name, rule_cat

    if item.category and item.category != "General":
        return merchant_name, item.category

    jev_cat, _ = classify_transaction_jev(merchant_name, item.amount, item.notes)
    resolved = jev_cat if jev_cat and jev_cat != "General" else auto_categorize(merchant_name)
    return merchant_name, resolved


def _cross_source_key(tx_date: date, amount: float | Decimal) -> tuple[date, Decimal]:
    """Build a normalized (date, 8dp amount) key for cross-source deduplication."""
    return (tx_date, Decimal(str(amount)).quantize(Decimal("0.00000001")))


def ingest_normalized_transactions(
    db: Session,
    account: Account,
    items: list[NormalizedTx] | tuple[NormalizedTx, ...],
) -> tuple[int, int, list[Transaction]]:
    """Deduplicate, categorize, persist, and reconcile normalized transactions."""
    if not items:
        return 0, 0, []

    active_rules = db.scalars(
        select(CategorizationRule)
        .where(CategorizationRule.is_active == True)
        .order_by(CategorizationRule.priority.asc(), CategorizationRule.id.asc())
    ).all()

    # Preload existing (date, amount) counts for this account so cross-source
    # statement uploads do not duplicate API/email-synced rows with different merchant text.
    tx_dates = {item.transaction_date for item in items}
    existing_rows = db.execute(
        select(Transaction.transaction_date, Transaction.amount).where(
            Transaction.account_id == account.id,
            Transaction.transaction_date.in_(tx_dates),
        )
    ).all()
    existing_counts: Counter[tuple[date, Decimal]] = Counter(
        _cross_source_key(row.transaction_date, row.amount) for row in existing_rows
    )
    batch_seen_counts: Counter[tuple[date, Decimal]] = Counter()

    created: list[Transaction] = []
    skipped = 0

    for item in items:
        raw_hash = generate_transaction_hash(
            tx_date=item.transaction_date,
            amount=item.amount,
            merchant=item.merchant,
            account_id=account.id,
        )
        if db.scalar(select(Transaction).where(Transaction.raw_hash == raw_hash)):
            skipped += 1
            continue

        cs_key = _cross_source_key(item.transaction_date, item.amount)
        batch_seen_counts[cs_key] += 1
        if batch_seen_counts[cs_key] <= existing_counts[cs_key]:
            skipped += 1
            continue

        merchant_name, tx_cat = _resolve_category_and_merchant(item, active_rules)
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


def sync_provider_feeds(
    db: Session,
    *,
    provider: str,
    feeds: list[ProviderAccountFeed],
) -> SyncResult:
    """Persist a list of ProviderAccountFeed contracts into Plutus uniformly."""
    today = date.today()
    summaries: list[SyncAccountSummary] = []
    total_created = 0
    total_skipped = 0
    snapshots_updated = 0

    for feed in feeds:
        spec = feed.account
        account = get_or_create_external_account(db, spec)

        if spec.current_balance is not None:
            upsert_account_snapshot(
                db,
                account=account,
                snapshot_date=today,
                balance=spec.current_balance,
                currency=spec.currency,
                source=provider,
                notes=spec.snapshot_notes,
            )
            snapshots_updated += 1

        created, skipped, _ = ingest_normalized_transactions(db, account, feed.transactions)
        total_created += created
        total_skipped += skipped
        summaries.append(
            SyncAccountSummary(
                account_id=account.id,
                account_name=account.name,
                institution=account.institution,
                currency=account.currency,
                balance=spec.current_balance,
                transactions_created=created,
                transactions_skipped=skipped,
            )
        )

    return SyncResult(
        provider=provider,
        accounts_synced=len(summaries),
        transactions_created=total_created,
        transactions_skipped=total_skipped,
        snapshots_updated=snapshots_updated,
        accounts=summaries,
    )
