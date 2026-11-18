"""Shared ingestion, deduplication, and balance snapshot helpers for all connectors."""

# ============================================================================
# 1. IMPORTS & CONSTANTS
# ============================================================================
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from typing import Optional, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Account, AssetSnapshot, CategorizationRule, Transaction
from app.schemas import SyncAccountSummary, SyncResult
from app.services.fx_service import get_exchange_rate
from app.services.jev_client import classify_transaction_jev_with_tags
from app.services.receipt_service import reconcile_unlinked_receipts
from app.services.rules_engine import evaluate_rules, normalize_tags, parse_rule_tags
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
    tags: tuple[str, ...] = ()


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

    def fetch_feeds(
        self,
        *,
        days: int = 30,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> list[ProviderAccountFeed]:
        """Fetch normalized account balances and transactions for the lookback or date window."""
        ...


def resolve_utc_sync_window(
    *,
    days: int = 30,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> tuple[datetime, datetime]:
    """Resolve an explicit (start_date, end_date) or relative days lookback into UTC datetimes."""
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError("start_date must be on or before end_date.")

    if end_date is not None:
        end_dt = datetime.combine(end_date, time(23, 59, 59), tzinfo=timezone.utc)
    else:
        end_dt = datetime.now(timezone.utc)

    if start_date is not None:
        start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    else:
        start_dt = end_dt - timedelta(days=days)

    return start_dt, end_dt


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
    *,
    skip_llm: bool = False,
) -> tuple[str, str, list[str], Optional[str]]:
    """Resolve final (merchant, category, extra_tags, recurrence) using rules, upstream category, and Jev."""
    desc = item.raw_description or item.merchant
    rule_cat, rename, rule_id = evaluate_rules(desc, item.amount, active_rules)
    merchant_name = (rename if rename else item.merchant)[:150]

    extra_tags: list[str] = []
    rule_recurrence: Optional[str] = None
    if rule_cat:
        if rule_id:
            matched = next((r for r in active_rules if r.id == rule_id), None)
            if matched:
                matched.match_count += 1
                extra_tags = parse_rule_tags(matched.target_tags)
                rule_recurrence = matched.target_recurrence
        resolved_cat = rule_cat
    elif item.category and item.category != "General":
        resolved_cat = item.category
    elif skip_llm:
        resolved_cat = auto_categorize(merchant_name)
        return merchant_name, resolved_cat, [], None
    else:
        jev_cat, _, jev_tags = classify_transaction_jev_with_tags(merchant_name, item.amount, item.notes)
        resolved_cat = jev_cat if jev_cat and jev_cat != "General" else auto_categorize(merchant_name)
        cat_lower = resolved_cat.strip().lower()
        return merchant_name, resolved_cat, [t for t in jev_tags if t.strip().lower() != cat_lower], None

    if not skip_llm and not item.tags and not extra_tags:
        _, _, jev_tags = classify_transaction_jev_with_tags(merchant_name, item.amount, item.notes)
        extra_tags = jev_tags

    cat_lower = resolved_cat.strip().lower()
    filtered_extra = [t for t in extra_tags if t.strip().lower() != cat_lower]
    return merchant_name, resolved_cat, filtered_extra, rule_recurrence


def _cross_source_key(tx_date: date, amount: float | Decimal) -> tuple[date, Decimal]:
    """Build a normalized (date, 8dp amount) key for cross-source deduplication."""
    return (tx_date, Decimal(str(amount)).quantize(Decimal("0.00000001")))


def ingest_normalized_transactions(
    db: Session,
    account: Account,
    items: list[NormalizedTx] | tuple[NormalizedTx, ...],
    *,
    skip_llm: bool = False,
) -> tuple[int, int, list[Transaction]]:
    """Deduplicate, categorize, persist, and reconcile normalized transactions."""
    if not items:
        account.last_synced_at = datetime.now(timezone.utc)
        db.commit()
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
    seen_hashes: set[str] = set()
    seen_external_notes: set[str] = set()
    existing_external_notes = set(
        n
        for n in db.scalars(
            select(Transaction.notes).where(
                Transaction.account_id == account.id,
                Transaction.notes.isnot(None),
            )
        ).all()
        if n and n.startswith(("akahu:", "wise:", "btc:"))
    )

    existing_btc_by_note = {
        tx.notes: tx
        for tx in db.scalars(
            select(Transaction).where(
                Transaction.account_id == account.id,
                Transaction.notes.like("btc:%"),
            )
        ).all()
        if tx.notes
    }

    created: list[Transaction] = []
    skipped = 0
    reconcile_needed = False

    for item in items:
        ext_note = (
            item.notes
            if (item.notes and item.notes.startswith(("akahu:", "wise:", "btc:")))
            else None
        )
        if ext_note and (ext_note in existing_external_notes or ext_note in seen_external_notes):
            if ext_note in existing_btc_by_note and ext_note not in seen_external_notes:
                seen_external_notes.add(ext_note)
                existing_tx = existing_btc_by_note[ext_note]
                date_changed = existing_tx.transaction_date != item.transaction_date
                if date_changed:
                    existing_tx.transaction_date = item.transaction_date
                    if existing_tx.linked_transaction_id is not None:
                        partner = db.get(Transaction, existing_tx.linked_transaction_id)
                        existing_tx.linked_transaction_id = None
                        if partner is not None:
                            partner.linked_transaction_id = None
                            if (partner.currency or "").strip().upper() == "NZD":
                                partner.exchange_rate = 1.0
                    reconcile_needed = True
                if date_changed or existing_tx.exchange_rate is None or float(existing_tx.exchange_rate) < 1.0:
                    tx_currency = (item.currency or account.currency or "BTC")[:10].upper()
                    resolved = get_exchange_rate(
                        db,
                        rate_date=item.transaction_date,
                        from_currency=tx_currency,
                        to_currency="NZD",
                    )
                    if resolved is not None and resolved[0] > 0:
                        existing_tx.exchange_rate = round(float(resolved[0]), 8)
                if existing_tx.linked_transaction_id is None:
                    reconcile_needed = True
            skipped += 1
            continue

        base_hash = generate_transaction_hash(
            tx_date=item.transaction_date,
            amount=item.amount,
            merchant=item.merchant,
            account_id=account.id,
        )
        hash_exists = (
            base_hash in seen_hashes
            or db.scalar(select(Transaction.id).where(Transaction.raw_hash == base_hash)) is not None
        )

        if hash_exists:
            if not ext_note:
                skipped += 1
                continue
            raw_hash = hashlib.sha256(f"{base_hash}:{ext_note}".encode("utf-8")).hexdigest()
            salted_exists = (
                raw_hash in seen_hashes
                or db.scalar(select(Transaction.id).where(Transaction.raw_hash == raw_hash)) is not None
            )
            if salted_exists:
                skipped += 1
                continue
        else:
            raw_hash = base_hash
            cs_key = _cross_source_key(item.transaction_date, item.amount)
            batch_seen_counts[cs_key] += 1
            if batch_seen_counts[cs_key] <= existing_counts[cs_key]:
                skipped += 1
                continue

        seen_hashes.add(raw_hash)
        if ext_note:
            seen_external_notes.add(ext_note)

        merchant_name, tx_cat, rule_tags, rule_recurrence = _resolve_category_and_merchant(
            item,
            active_rules,
            skip_llm=skip_llm,
        )
        tx_currency = (item.currency or account.currency or "NZD")[:10].upper()
        tx_fx_rate = item.exchange_rate
        if tx_fx_rate is None and tx_currency != "NZD":
            resolved = get_exchange_rate(
                db,
                rate_date=item.transaction_date,
                from_currency=tx_currency,
                to_currency="NZD",
            )
            if resolved is not None:
                tx_fx_rate = resolved[0]

        tx = Transaction(
            transaction_date=item.transaction_date,
            account_id=account.id,
            amount=item.amount,
            currency=tx_currency[:3],
            merchant=merchant_name,
            category=tx_cat[:50],
            raw_description=item.raw_description,
            raw_hash=raw_hash,
            exchange_rate=tx_fx_rate,
            fee_amount=item.fee_amount,
            tags=json.dumps(normalize_tags([*item.tags, *rule_tags])),
            recurrence=rule_recurrence,
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
    elif reconcile_needed:
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
    normalized_currency = (currency or account.currency or "NZD")[:10].upper()
    fx_rate = 1.0
    if normalized_currency != "NZD":
        resolved = get_exchange_rate(
            db,
            rate_date=snapshot_date,
            from_currency=normalized_currency,
            to_currency="NZD",
        )
        if resolved is not None:
            fx_rate = resolved[0]
    balance_nzd = round(float(balance) * fx_rate, 2)

    existing = db.scalar(
        select(AssetSnapshot).where(
            AssetSnapshot.account_id == account.id,
            AssetSnapshot.snapshot_date == snapshot_date,
        )
    )
    if existing:
        existing.balance = balance
        existing.exchange_rate = fx_rate
        existing.balance_nzd = balance_nzd
        existing.currency = normalized_currency[:3]
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
        currency=normalized_currency[:3],
        exchange_rate=fx_rate,
        balance_nzd=balance_nzd,
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
    skip_llm: bool = False,
) -> SyncResult:
    """Persist a list of ProviderAccountFeed contracts into Plutus uniformly."""
    today = datetime.now(ZoneInfo(settings.timezone)).date()
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

        created, skipped, _ = ingest_normalized_transactions(
            db,
            account,
            feed.transactions,
            skip_llm=skip_llm,
        )
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
