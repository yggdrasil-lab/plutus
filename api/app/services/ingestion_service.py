"""Shared ingestion, deduplication, and balance snapshot helpers for all connectors."""

# ============================================================================
# 1. IMPORTS & CONSTANTS
# ============================================================================
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from functools import partial
import hashlib
import json
from typing import Any, Callable, Optional, Protocol, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Account, AssetSnapshot, CategorizationRule, Transaction
from app.schemas import SyncAccountSummary, SyncResult
from app.services.ai_gateway import AiGateway
from app.services.cross_source_reconciler import (
    CrossSourceReconciler,
    default_cross_source_reconcilers,
    extract_external_note_keys,
    refresh_existing_btc_transaction,
)
from app.services.fx_service import FxRateFetcher, HttpFxRateFetcher, get_exchange_rate
from app.services.jev_client import classify_transaction_jev_with_tags, get_extra_jev_categories
from app.services.ledger_entry import (
    collect_existing_tags,
    generate_transaction_hash,
    normalize_tags,
    parse_rule_tags,
    strip_redundant_tags,
)
from app.services.receipt_ingestion import reconcile_unlinked_receipts
from app.services.rules_engine import evaluate_rules
from app.services.statement_parser import auto_categorize
from app.services.transfer_reconciliation import apply_counterpart_account_for_rule
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


def _extract_rule_match(
    rule_id: Optional[int],
    active_rules: list[CategorizationRule],
) -> tuple[list[str], Optional[str], Optional[CategorizationRule]]:
    if not rule_id:
        return [], None, None
    matched_rule = next((r for r in active_rules if r.id == rule_id), None)
    if not matched_rule:
        return [], None, None
    matched_rule.match_count += 1
    return parse_rule_tags(matched_rule.target_tags), matched_rule.target_recurrence, matched_rule


def _resolve_category_and_merchant(
    item: NormalizedTx,
    active_rules: list[CategorizationRule],
    *,
    skip_llm: bool = False,
    existing_tags: list[str] | tuple[str, ...] | None = None,
    extra_categories: list[str] | tuple[str, ...] | None = None,
    gateway: AiGateway | None = None,
) -> tuple[str, str, list[str], Optional[str], Optional[CategorizationRule]]:
    """Resolve final (merchant, category, extra_tags, recurrence, matched_rule)."""
    desc = item.raw_description or item.merchant
    rule_cat, rename, rule_id = evaluate_rules(desc, item.amount, active_rules)
    merchant_name = (rename if rename else item.merchant)[:150]

    extra_tags: list[str] = []
    rule_recurrence: Optional[str] = None
    matched_rule: Optional[CategorizationRule] = None
    if rule_cat:
        extra_tags, rule_recurrence, matched_rule = _extract_rule_match(rule_id, active_rules)
        resolved_cat = rule_cat
    elif item.category and item.category != "General":
        resolved_cat = item.category
    elif skip_llm:
        return merchant_name, auto_categorize(merchant_name), [], None, None
    else:
        jev_cat, _, jev_tags = classify_transaction_jev_with_tags(
            merchant_name,
            item.amount,
            item.notes,
            existing_tags=existing_tags,
            extra_categories=extra_categories,
            gateway=gateway,
        )
        resolved_cat = jev_cat if jev_cat and jev_cat != "General" else auto_categorize(merchant_name)
        return merchant_name, resolved_cat, strip_redundant_tags(jev_tags, resolved_cat), None, None

    if not skip_llm and not item.tags and not extra_tags:
        _, _, jev_tags = classify_transaction_jev_with_tags(
            merchant_name,
            item.amount,
            item.notes,
            existing_tags=existing_tags,
            extra_categories=extra_categories,
            gateway=gateway,
        )
        extra_tags = jev_tags

    filtered_extra = strip_redundant_tags(extra_tags, resolved_cat)
    return merchant_name, resolved_cat, filtered_extra, rule_recurrence, matched_rule


_NOTE_LOOKUP_CHUNK_SIZE = 200


def _cross_source_key(tx_date: date, amount: float | Decimal) -> tuple[date, Decimal]:
    """Build a normalized (date, 8dp amount) key for cross-source deduplication."""
    return (tx_date, Decimal(str(amount)).quantize(Decimal("0.00000001")))


def _amount_decimal_key(amount: float | Decimal) -> Decimal:
    """Normalize transaction amount to 8 decimal places for exact comparison."""
    return Decimal(str(amount)).quantize(Decimal("0.00000001"))


@dataclass
class _IngestionBatchContext:
    active_rules: list[CategorizationRule]
    existing_tags: list[str]
    extra_categories: list[str]
    window_existing_txs: list[Transaction]
    existing_counts: Counter[tuple[date, Decimal]]
    existing_external_notes: set[str]
    existing_tx_by_note: dict[str, Transaction]
    batch_seen_counts: Counter[tuple[date, Decimal]] = field(default_factory=Counter)
    consumed_existing_ids: set[int] = field(default_factory=set)
    seen_hashes: set[str] = field(default_factory=set)
    seen_external_notes: set[str] = field(default_factory=set)
    created: list[Transaction] = field(default_factory=list)
    matched_existing: list[Transaction] = field(default_factory=list)
    matched_tx_rules: list[tuple[Transaction, CategorizationRule]] = field(default_factory=list)
    skipped: int = 0
    reconcile_needed: bool = False


def _collect_reconciler_keys(
    items: Sequence[NormalizedTx],
    reconciler: CrossSourceReconciler,
) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item.notes or not item.notes.strip():
            continue
        for key in reconciler.extract_note_keys(item.notes.strip()):
            if key and key not in seen:
                seen.add(key)
                keys.append(key)
    return keys


def _collect_note_lookup_clauses(
    items: Sequence[NormalizedTx],
    reconcilers: Sequence[CrossSourceReconciler],
) -> list[Any]:
    incoming_keys: list[str] = []
    seen_incoming: set[str] = set()
    for item in items:
        for key in extract_external_note_keys(item.notes, reconcilers):
            if key not in seen_incoming:
                seen_incoming.add(key)
                incoming_keys.append(key)
    if not incoming_keys:
        return []

    clauses: list[Any] = []
    uncovered_keys: list[str] = []
    for reconciler in reconcilers:
        rec_keys = _collect_reconciler_keys(items, reconciler)
        if not rec_keys:
            continue
        rec_clauses = reconciler.build_note_lookup_clauses(Transaction.notes, incoming_keys)
        if rec_clauses:
            clauses.extend(rec_clauses)
        else:
            uncovered_keys.extend(k for k in rec_keys if k not in uncovered_keys)
    for idx in range(0, len(uncovered_keys), _NOTE_LOOKUP_CHUNK_SIZE):
        clauses.append(Transaction.notes.in_(uncovered_keys[idx : idx + _NOTE_LOOKUP_CHUNK_SIZE]))
    return clauses


def _load_existing_tx_by_note(
    db: Session,
    account: Account,
    items: Sequence[NormalizedTx],
    reconcilers: Sequence[CrossSourceReconciler],
) -> tuple[set[str], dict[str, Transaction]]:
    clauses = _collect_note_lookup_clauses(items, reconcilers)
    if not clauses:
        return set(), {}

    existing_external_notes: set[str] = set()
    existing_tx_by_note: dict[str, Transaction] = {}
    for idx in range(0, len(clauses), _NOTE_LOOKUP_CHUNK_SIZE):
        clause_chunk = clauses[idx : idx + _NOTE_LOOKUP_CHUNK_SIZE]
        rows = db.scalars(
            select(Transaction).where(
                Transaction.account_id == account.id,
                Transaction.notes.isnot(None),
                or_(*clause_chunk),
            )
        ).all()
        for tx in rows:
            for ext_key in extract_external_note_keys(tx.notes, reconcilers):
                existing_external_notes.add(ext_key)
                existing_tx_by_note.setdefault(ext_key, tx)
    return existing_external_notes, existing_tx_by_note


def _load_ingestion_batch_context(
    db: Session,
    account: Account,
    items: list[NormalizedTx] | tuple[NormalizedTx, ...],
    skip_llm: bool,
    reconcilers: Sequence[CrossSourceReconciler],
) -> _IngestionBatchContext:
    active_rules = list(
        db.scalars(
            select(CategorizationRule)
            .where(CategorizationRule.is_active == True)
            .order_by(CategorizationRule.priority.asc(), CategorizationRule.id.asc())
        ).all()
    )
    tx_dates = {item.transaction_date for item in items}
    window_existing_txs = list(
        db.scalars(
            select(Transaction).where(
                Transaction.account_id == account.id,
                Transaction.transaction_date >= min(tx_dates) - timedelta(days=3),
                Transaction.transaction_date <= max(tx_dates) + timedelta(days=3),
            )
        ).all()
    )
    existing_counts: Counter[tuple[date, Decimal]] = Counter(
        _cross_source_key(row.transaction_date, row.amount)
        for row in window_existing_txs
        if row.transaction_date in tx_dates
    )
    existing_external_notes, existing_tx_by_note = _load_existing_tx_by_note(
        db, account, items, reconcilers
    )
    return _IngestionBatchContext(
        active_rules=active_rules,
        existing_tags=collect_existing_tags(db) if not skip_llm else [],
        extra_categories=get_extra_jev_categories(db) if not skip_llm else [],
        window_existing_txs=window_existing_txs,
        existing_counts=existing_counts,
        existing_external_notes=existing_external_notes,
        existing_tx_by_note=existing_tx_by_note,
    )


def _refresh_existing_btc_transaction(
    db: Session,
    account: Account,
    item: NormalizedTx,
    existing_tx: Transaction,
    *,
    fetcher: Optional[FxRateFetcher] = None,
) -> bool:
    """Backward-compatible wrapper delegating to cross_source_reconciler.refresh_existing_btc_transaction."""
    return refresh_existing_btc_transaction(
        db,
        account,
        item,
        existing_tx,
        fetcher=fetcher,
        exchange_rate_fn=get_exchange_rate,
    )


def _handle_duplicate_external_note(
    db: Session,
    account: Account,
    item: NormalizedTx,
    item_ext_keys: Sequence[str],
    ctx: _IngestionBatchContext,
    reconcilers: Sequence[CrossSourceReconciler],
    *,
    exchange_rate_fn: Callable[..., Any],
) -> bool:
    matched_key = next(
        (
            k
            for k in item_ext_keys
            if k in ctx.existing_external_notes or k in ctx.seen_external_notes
        ),
        None,
    )
    if matched_key is None:
        return False
    existing_tx = ctx.existing_tx_by_note.get(matched_key)
    if existing_tx is not None and matched_key not in ctx.seen_external_notes:
        ctx.matched_existing.append(existing_tx)
        for reconciler in reconcilers:
            res = reconciler.on_duplicate_external_note(
                db,
                account,
                existing_tx,
                item,
                exchange_rate_fn=exchange_rate_fn,
            )
            if res.refreshed:
                ctx.reconcile_needed = True
            if res.handled:
                ctx.seen_external_notes.add(matched_key)
                break
    ctx.skipped += 1
    return True


def _handle_cross_source_reconciliation(
    db: Session,
    account: Account,
    item: NormalizedTx,
    ctx: _IngestionBatchContext,
    reconcilers: Sequence[CrossSourceReconciler],
    *,
    skip_llm: bool,
    gateway: AiGateway | None,
) -> bool:
    available_window_txs = [
        tx for tx in ctx.window_existing_txs if tx.id not in ctx.consumed_existing_ids
    ]

    def _resolve_category(tx_item: NormalizedTx, *, gateway: AiGateway | None = None):
        return _resolve_category_and_merchant(
            tx_item,
            ctx.active_rules,
            skip_llm=skip_llm,
            existing_tags=ctx.existing_tags,
            extra_categories=ctx.extra_categories,
            gateway=gateway,
        )

    for reconciler in reconcilers:
        res = reconciler.reconcile_cross_source(
            db,
            account,
            item,
            available_window_txs,
            ctx.existing_tx_by_note,
            category_resolver=_resolve_category,
            gateway=gateway,
        )
        if not res.matched:
            continue
        if res.matched_tx is not None:
            ctx.consumed_existing_ids.add(res.matched_tx.id)
            ctx.matched_existing.append(res.matched_tx)
            for key in extract_external_note_keys(res.matched_tx.notes, reconcilers):
                ctx.existing_external_notes.add(key)
                ctx.seen_external_notes.add(key)
                ctx.existing_tx_by_note[key] = res.matched_tx
        if res.matched_rules:
            ctx.matched_tx_rules.extend(res.matched_rules)
        if res.enriched:
            ctx.reconcile_needed = True
        ctx.skipped += 1
        return True
    return False


def _resolve_unique_raw_hash(
    db: Session,
    account: Account,
    item: NormalizedTx,
    ext_note: Optional[str],
    ctx: _IngestionBatchContext,
) -> Optional[str]:
    base_hash = generate_transaction_hash(
        tx_date=item.transaction_date,
        amount=item.amount,
        merchant=item.merchant,
        account_id=account.id,
    )
    hash_exists = (
        base_hash in ctx.seen_hashes
        or db.scalar(select(Transaction.id).where(Transaction.raw_hash == base_hash)) is not None
    )
    if hash_exists:
        if not ext_note:
            existing_by_hash = db.scalar(select(Transaction).where(Transaction.raw_hash == base_hash))
            if existing_by_hash is not None:
                ctx.matched_existing.append(existing_by_hash)
            ctx.skipped += 1
            return None
        raw_hash = hashlib.sha256(f"{base_hash}:{ext_note}".encode("utf-8")).hexdigest()
        salted_exists = (
            raw_hash in ctx.seen_hashes
            or db.scalar(select(Transaction.id).where(Transaction.raw_hash == raw_hash)) is not None
        )
        if salted_exists:
            existing_by_hash = db.scalar(select(Transaction).where(Transaction.raw_hash == raw_hash))
            if existing_by_hash is not None:
                ctx.matched_existing.append(existing_by_hash)
            ctx.skipped += 1
            return None
        return raw_hash

    cs_key = _cross_source_key(item.transaction_date, item.amount)
    ctx.batch_seen_counts[cs_key] += 1
    if ctx.batch_seen_counts[cs_key] <= ctx.existing_counts[cs_key]:
        matching_exact = next(
            (
                tx
                for tx in ctx.window_existing_txs
                if tx.transaction_date == item.transaction_date
                and _amount_decimal_key(tx.amount) == _amount_decimal_key(item.amount)
                and tx.id not in ctx.consumed_existing_ids
            ),
            None,
        )
        if matching_exact is not None:
            ctx.consumed_existing_ids.add(matching_exact.id)
            ctx.matched_existing.append(matching_exact)
        ctx.skipped += 1
        return None
    return base_hash


def _persist_new_ingested_tx(
    db: Session,
    account: Account,
    item: NormalizedTx,
    raw_hash: str,
    ext_note: Optional[str],
    ctx: _IngestionBatchContext,
    skip_llm: bool,
    gateway: AiGateway | None,
    *,
    fetcher: Optional[FxRateFetcher] = None,
) -> None:
    ctx.seen_hashes.add(raw_hash)
    if ext_note:
        ctx.seen_external_notes.add(ext_note)

    merchant_name, tx_cat, rule_tags, rule_recurrence, matched_rule = _resolve_category_and_merchant(
        item,
        ctx.active_rules,
        skip_llm=skip_llm,
        existing_tags=ctx.existing_tags,
        extra_categories=ctx.extra_categories,
        gateway=gateway,
    )
    tx_currency = (item.currency or account.currency or "NZD")[:10].upper()
    tx_fx_rate = item.exchange_rate
    if tx_fx_rate is None and tx_currency != "NZD":
        resolved = get_exchange_rate(
            db,
            rate_date=item.transaction_date,
            from_currency=tx_currency,
            to_currency="NZD",
            fetcher=fetcher,
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
        loan_id=matched_rule.loan_id if matched_rule is not None else None,
        notes=item.notes,
    )
    db.add(tx)
    ctx.created.append(tx)
    if matched_rule is not None and matched_rule.counterpart_account_id is not None:
        ctx.matched_tx_rules.append((tx, matched_rule))


def _finalize_ingestion_batch(
    db: Session,
    account: Account,
    ctx: _IngestionBatchContext,
    *,
    fetcher: Optional[FxRateFetcher] = None,
) -> None:
    account.last_synced_at = datetime.now(timezone.utc)
    db.commit()
    for tx in ctx.created:
        db.refresh(tx)
    for tx in ctx.matched_existing:
        db.refresh(tx)
    if not (ctx.created or ctx.reconcile_needed):
        return
    reconcile_unlinked_receipts(db)
    reconcile_transfers(db, fetcher=fetcher)
    if not ctx.matched_tx_rules:
        return
    counterpart_updated = False
    for tx, matched_rule in ctx.matched_tx_rules:
        db.refresh(tx)
        if apply_counterpart_account_for_rule(db, tx, matched_rule, fetcher=fetcher):
            counterpart_updated = True
    if counterpart_updated:
        db.commit()
        for tx in ctx.created:
            db.refresh(tx)
        for tx in ctx.matched_existing:
            db.refresh(tx)


def ingest_normalized_transactions_detailed(
    db: Session,
    account: Account,
    items: list[NormalizedTx] | tuple[NormalizedTx, ...],
    *,
    skip_llm: bool = False,
    gateway: AiGateway | None = None,
    fx_fetcher: Optional[FxRateFetcher] = None,
    reconcilers: Optional[Sequence[CrossSourceReconciler]] = None,
) -> tuple[int, int, list[Transaction], list[Transaction]]:
    """Deduplicate, categorize, persist, enrich, and reconcile normalized transactions."""
    if not items:
        account.last_synced_at = datetime.now(timezone.utc)
        db.commit()
        return 0, 0, [], []

    active_reconcilers = (
        default_cross_source_reconcilers() if reconcilers is None else tuple(reconcilers)
    )
    fetcher_ctx = (
        nullcontext(fx_fetcher)
        if fx_fetcher is not None
        else HttpFxRateFetcher(wise_api_token=settings.wise_api_token)
    )
    with fetcher_ctx as active_fetcher:
        ctx = _load_ingestion_batch_context(db, account, items, skip_llm, active_reconcilers)
        exchange_rate_fn = partial(get_exchange_rate, fetcher=active_fetcher)
        for item in items:
            item_ext_keys = extract_external_note_keys(item.notes, active_reconcilers)
            ext_note = item_ext_keys[0] if item_ext_keys else None
            if _handle_duplicate_external_note(
                db,
                account,
                item,
                item_ext_keys,
                ctx,
                active_reconcilers,
                exchange_rate_fn=exchange_rate_fn,
            ):
                continue
            if _handle_cross_source_reconciliation(
                db,
                account,
                item,
                ctx,
                active_reconcilers,
                skip_llm=skip_llm,
                gateway=gateway,
            ):
                continue
            raw_hash = _resolve_unique_raw_hash(db, account, item, ext_note, ctx)
            if raw_hash is None:
                continue
            _persist_new_ingested_tx(
                db,
                account,
                item,
                raw_hash,
                ext_note,
                ctx,
                skip_llm,
                gateway,
                fetcher=active_fetcher,
            )

        _finalize_ingestion_batch(db, account, ctx, fetcher=active_fetcher)
    return len(ctx.created), ctx.skipped, ctx.created, ctx.matched_existing


def ingest_normalized_transactions(
    db: Session,
    account: Account,
    items: list[NormalizedTx] | tuple[NormalizedTx, ...],
    *,
    skip_llm: bool = False,
    gateway: AiGateway | None = None,
    fx_fetcher: Optional[FxRateFetcher] = None,
    reconcilers: Optional[Sequence[CrossSourceReconciler]] = None,
) -> tuple[int, int, list[Transaction]]:
    """Deduplicate, categorize, persist, and reconcile normalized transactions."""
    created_count, skipped_count, created, _ = ingest_normalized_transactions_detailed(
        db,
        account,
        items,
        skip_llm=skip_llm,
        gateway=gateway,
        fx_fetcher=fx_fetcher,
        reconcilers=reconcilers,
    )
    return created_count, skipped_count, created


def upsert_account_snapshot(
    db: Session,
    *,
    account: Account,
    snapshot_date: date,
    balance: float,
    currency: str,
    source: str,
    notes: Optional[str] = None,
    exchange_rate: Optional[float] = None,
    preserve_existing_notes_if_none: bool = True,
    fx_fetcher: Optional[FxRateFetcher] = None,
) -> AssetSnapshot:
    """Insert or update the balance snapshot for an account on a given date."""
    normalized_currency = (currency or account.currency or "NZD")[:10].upper()
    fx_rate = 1.0
    if normalized_currency != "NZD":
        if exchange_rate is not None and float(exchange_rate) > 0:
            fx_rate = float(exchange_rate)
        else:
            resolved = get_exchange_rate(
                db,
                rate_date=snapshot_date,
                from_currency=normalized_currency,
                to_currency="NZD",
                fetcher=fx_fetcher,
            )
            if resolved is not None and resolved[0] > 0:
                fx_rate = float(resolved[0])
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
        if notes or not preserve_existing_notes_if_none:
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
    gateway: AiGateway | None = None,
    reconcilers: Optional[Sequence[CrossSourceReconciler]] = None,
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
            gateway=gateway,
            reconcilers=reconcilers,
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
