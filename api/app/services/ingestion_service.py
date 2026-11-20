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
import re
from typing import Optional, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Account, AssetSnapshot, CategorizationRule, Transaction
from app.schemas import SyncAccountSummary, SyncResult
from app.services.fx_service import get_exchange_rate
from app.services.jev_client import classify_transaction_jev_with_tags, get_extra_jev_categories
from app.services.receipt_service import reconcile_unlinked_receipts
from app.services.rules_engine import (
    apply_counterpart_account_for_rule,
    collect_existing_tags,
    evaluate_rules,
    normalize_tags,
    parse_rule_tags,
    strip_redundant_tags,
)
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
    existing_tags: list[str] | tuple[str, ...] | None = None,
    extra_categories: list[str] | tuple[str, ...] | None = None,
) -> tuple[str, str, list[str], Optional[str], Optional[CategorizationRule]]:
    """Resolve final (merchant, category, extra_tags, recurrence, matched_rule)."""
    desc = item.raw_description or item.merchant
    rule_cat, rename, rule_id = evaluate_rules(desc, item.amount, active_rules)
    merchant_name = (rename if rename else item.merchant)[:150]

    extra_tags: list[str] = []
    rule_recurrence: Optional[str] = None
    matched_rule: Optional[CategorizationRule] = None
    if rule_cat:
        if rule_id:
            matched_rule = next((r for r in active_rules if r.id == rule_id), None)
            if matched_rule:
                matched_rule.match_count += 1
                extra_tags = parse_rule_tags(matched_rule.target_tags)
                rule_recurrence = matched_rule.target_recurrence
        resolved_cat = rule_cat
    elif item.category and item.category != "General":
        resolved_cat = item.category
    elif skip_llm:
        resolved_cat = auto_categorize(merchant_name)
        return merchant_name, resolved_cat, [], None, None
    else:
        jev_cat, _, jev_tags = classify_transaction_jev_with_tags(
            merchant_name,
            item.amount,
            item.notes,
            existing_tags=existing_tags,
            extra_categories=extra_categories,
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
        )
        extra_tags = jev_tags

    filtered_extra = strip_redundant_tags(extra_tags, resolved_cat)
    return merchant_name, resolved_cat, filtered_extra, rule_recurrence, matched_rule


EXTERNAL_NOTE_PREFIXES = ("akahu:", "wise:", "btc:", "dbs:")
_DBS_REF_TOKEN_RE = re.compile(r"(?:^|[\s|;])(dbs:[^\s|;]+)")


def _cross_source_key(tx_date: date, amount: float | Decimal) -> tuple[date, Decimal]:
    """Build a normalized (date, 8dp amount) key for cross-source deduplication."""
    return (tx_date, Decimal(str(amount)).quantize(Decimal("0.00000001")))


def _amount_decimal_key(amount: float | Decimal) -> Decimal:
    """Normalize transaction amount to 8 decimal places for exact comparison."""
    return Decimal(str(amount)).quantize(Decimal("0.00000001"))


def _extract_external_note_keys(notes: Optional[str]) -> list[str]:
    """Extract canonical external-id keys from a Transaction.notes string."""
    if not notes:
        return []
    stripped = notes.strip()
    keys: list[str] = []
    if stripped.startswith(("akahu:", "wise:", "btc:")):
        keys.append(stripped)
        return keys
    for match in _DBS_REF_TOKEN_RE.finditer(stripped):
        tok = match.group(1)
        if tok not in keys:
            keys.append(tok)
    return keys


def _is_dbs_alert_notes(notes: Optional[str]) -> bool:
    """Return True if notes indicate a real-time DBS email alert source."""
    if not notes:
        return False
    stripped = notes.strip()
    return (
        stripped == "dbs_email_alert"
        or "dbs_email_alert" in stripped
        or bool(_DBS_REF_TOKEN_RE.search(stripped))
    )


def _is_generic_dbs_merchant(merchant: Optional[str]) -> bool:
    """Return True when a DBS statement merchant is generic placeholder text like 'Advice'."""
    cleaned = (merchant or "").strip().lower()
    if not cleaned:
        return True
    return cleaned.startswith("advice") or cleaned in {
        "dbs transaction",
        "bill payment",
        "funds transfer",
        "fast payment",
        "unknown",
    }


def _find_dbs_cross_source_match(
    item: NormalizedTx,
    item_currency: str,
    window_existing_txs: list[Transaction],
    consumed_existing_ids: set[int],
) -> Optional[Transaction]:
    """Find a +/- 3-day cross-source match between DBS alerts and statement rows."""
    incoming_is_dbs = _is_dbs_alert_notes(item.notes)
    item_amt_key = _amount_decimal_key(item.amount)
    candidates: list[tuple[int, int, Transaction]] = []

    for tx in window_existing_txs:
        if tx.id in consumed_existing_ids:
            continue
        if (tx.currency or "").strip().upper() != item_currency:
            continue
        if _amount_decimal_key(tx.amount) != item_amt_key:
            continue
        day_diff = abs((tx.transaction_date - item.transaction_date).days)
        if day_diff > 3:
            continue

        tx_is_dbs = _is_dbs_alert_notes(tx.notes)
        tx_is_other_api = bool(
            tx.notes and tx.notes.strip().startswith(("akahu:", "wise:", "btc:"))
        )
        if incoming_is_dbs:
            # Incoming DBS alert can match a non-alert statement/manual row (or legacy unref alert)
            if tx_is_other_api or tx_is_dbs:
                continue
        else:
            # Incoming statement row can match an existing DBS alert row
            if not tx_is_dbs:
                continue

        candidates.append((day_diff, tx.id, tx))

    if not candidates:
        return None
    candidates.sort(key=lambda row: (row[0], row[1]))
    return candidates[0][2]


def ingest_normalized_transactions_detailed(
    db: Session,
    account: Account,
    items: list[NormalizedTx] | tuple[NormalizedTx, ...],
    *,
    skip_llm: bool = False,
) -> tuple[int, int, list[Transaction], list[Transaction]]:
    """Deduplicate, categorize, persist, enrich, and reconcile normalized transactions."""
    if not items:
        account.last_synced_at = datetime.now(timezone.utc)
        db.commit()
        return 0, 0, [], []

    active_rules = db.scalars(
        select(CategorizationRule)
        .where(CategorizationRule.is_active == True)
        .order_by(CategorizationRule.priority.asc(), CategorizationRule.id.asc())
    ).all()
    existing_tags = collect_existing_tags(db) if not skip_llm else []
    extra_categories = get_extra_jev_categories(db) if not skip_llm else []

    # Preload existing (date, amount) counts for exact-date cross-source dedup AND
    # a +/- 3-day window for DBS alert <-> statement settlement matching.
    tx_dates = {item.transaction_date for item in items}
    min_window_date = min(tx_dates) - timedelta(days=3)
    max_window_date = max(tx_dates) + timedelta(days=3)
    window_existing_txs = list(
        db.scalars(
            select(Transaction).where(
                Transaction.account_id == account.id,
                Transaction.transaction_date >= min_window_date,
                Transaction.transaction_date <= max_window_date,
            )
        ).all()
    )
    existing_counts: Counter[tuple[date, Decimal]] = Counter(
        _cross_source_key(row.transaction_date, row.amount)
        for row in window_existing_txs
        if row.transaction_date in tx_dates
    )
    batch_seen_counts: Counter[tuple[date, Decimal]] = Counter()
    consumed_existing_ids: set[int] = set()
    seen_hashes: set[str] = set()
    seen_external_notes: set[str] = set()

    existing_external_notes: set[str] = set()
    existing_tx_by_note: dict[str, Transaction] = {}
    for tx in db.scalars(
        select(Transaction).where(
            Transaction.account_id == account.id,
            Transaction.notes.isnot(None),
        )
    ).all():
        for ext_key in _extract_external_note_keys(tx.notes):
            existing_external_notes.add(ext_key)
            existing_tx_by_note.setdefault(ext_key, tx)

    existing_btc_by_note = {
        key: tx
        for key, tx in existing_tx_by_note.items()
        if key.startswith("btc:")
    }

    created: list[Transaction] = []
    matched_existing: list[Transaction] = []
    matched_tx_rules: list[tuple[Transaction, CategorizationRule]] = []
    skipped = 0
    reconcile_needed = False

    for item in items:
        item_ext_keys = _extract_external_note_keys(item.notes)
        ext_note = item_ext_keys[0] if item_ext_keys else None
        if ext_note and (ext_note in existing_external_notes or ext_note in seen_external_notes):
            if ext_note in existing_tx_by_note and ext_note not in seen_external_notes:
                matched_existing.append(existing_tx_by_note[ext_note])
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

        item_currency = (item.currency or account.currency or "NZD")[:3].upper()
        dbs_match = _find_dbs_cross_source_match(
            item,
            item_currency,
            window_existing_txs,
            consumed_existing_ids,
        )
        if dbs_match is not None:
            consumed_existing_ids.add(dbs_match.id)
            matched_existing.append(dbs_match)
            incoming_is_dbs = _is_dbs_alert_notes(item.notes)
            if incoming_is_dbs:
                if item.notes:
                    if not dbs_match.notes:
                        dbs_match.notes = item.notes
                    elif item.notes not in dbs_match.notes:
                        if item.notes.startswith("dbs:"):
                            dbs_match.notes = f"{item.notes} | {dbs_match.notes}"
                        else:
                            dbs_match.notes = f"{dbs_match.notes} | {item.notes}"
                    for k in _extract_external_note_keys(dbs_match.notes):
                        existing_external_notes.add(k)
                        seen_external_notes.add(k)
                        existing_tx_by_note[k] = dbs_match

                if _is_generic_dbs_merchant(dbs_match.merchant) and not _is_generic_dbs_merchant(item.merchant):
                    merchant_name, tx_cat, rule_tags, rule_recurrence, matched_rule = _resolve_category_and_merchant(
                        item,
                        active_rules,
                        skip_llm=skip_llm,
                        existing_tags=existing_tags,
                        extra_categories=extra_categories,
                    )
                    dbs_match.merchant = merchant_name
                    if item.raw_description and item.raw_description not in (dbs_match.raw_description or ""):
                        dbs_match.raw_description = (
                            f"{dbs_match.raw_description} | {item.raw_description}"
                            if dbs_match.raw_description
                            else item.raw_description
                        )
                    if (dbs_match.category or "General") == "General" or tx_cat == "Transfers":
                        dbs_match.category = tx_cat[:50]
                    if rule_recurrence and not dbs_match.recurrence:
                        dbs_match.recurrence = rule_recurrence
                    existing_item_tags = parse_rule_tags(dbs_match.tags)
                    merged_tags = normalize_tags([*existing_item_tags, *item.tags, *rule_tags])
                    dbs_match.tags = json.dumps(merged_tags)
                    if matched_rule is not None and matched_rule.loan_id is not None and dbs_match.loan_id is None:
                        dbs_match.loan_id = matched_rule.loan_id
                    if matched_rule is not None and matched_rule.counterpart_account_id is not None:
                        matched_tx_rules.append((dbs_match, matched_rule))
                elif item.category == "Transfers" and (dbs_match.category or "General") == "General":
                    dbs_match.category = "Transfers"
            else:
                if _is_generic_dbs_merchant(dbs_match.merchant) and not _is_generic_dbs_merchant(item.merchant):
                    dbs_match.merchant = item.merchant[:150]
                if item.raw_description and not _is_generic_dbs_merchant(item.raw_description) and item.raw_description not in (dbs_match.raw_description or ""):
                    dbs_match.raw_description = (
                        f"{dbs_match.raw_description} | {item.raw_description}"
                        if dbs_match.raw_description
                        else item.raw_description
                    )
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
                existing_by_hash = db.scalar(
                    select(Transaction).where(Transaction.raw_hash == base_hash)
                )
                if existing_by_hash is not None:
                    matched_existing.append(existing_by_hash)
                skipped += 1
                continue
            raw_hash = hashlib.sha256(f"{base_hash}:{ext_note}".encode("utf-8")).hexdigest()
            salted_exists = (
                raw_hash in seen_hashes
                or db.scalar(select(Transaction.id).where(Transaction.raw_hash == raw_hash)) is not None
            )
            if salted_exists:
                existing_by_hash = db.scalar(
                    select(Transaction).where(Transaction.raw_hash == raw_hash)
                )
                if existing_by_hash is not None:
                    matched_existing.append(existing_by_hash)
                skipped += 1
                continue
        else:
            raw_hash = base_hash
            cs_key = _cross_source_key(item.transaction_date, item.amount)
            batch_seen_counts[cs_key] += 1
            if batch_seen_counts[cs_key] <= existing_counts[cs_key]:
                matching_exact = next(
                    (
                        tx
                        for tx in window_existing_txs
                        if tx.transaction_date == item.transaction_date
                        and _amount_decimal_key(tx.amount) == _amount_decimal_key(item.amount)
                        and tx.id not in consumed_existing_ids
                    ),
                    None,
                )
                if matching_exact is not None:
                    consumed_existing_ids.add(matching_exact.id)
                    matched_existing.append(matching_exact)
                skipped += 1
                continue

        seen_hashes.add(raw_hash)
        if ext_note:
            seen_external_notes.add(ext_note)

        merchant_name, tx_cat, rule_tags, rule_recurrence, matched_rule = _resolve_category_and_merchant(
            item,
            active_rules,
            skip_llm=skip_llm,
            existing_tags=existing_tags,
            extra_categories=extra_categories,
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
            loan_id=matched_rule.loan_id if matched_rule is not None else None,
            notes=item.notes,
        )
        db.add(tx)
        created.append(tx)
        if matched_rule is not None and matched_rule.counterpart_account_id is not None:
            matched_tx_rules.append((tx, matched_rule))

    account.last_synced_at = datetime.now(timezone.utc)
    db.commit()
    for tx in created:
        db.refresh(tx)
    for tx in matched_existing:
        db.refresh(tx)

    if created or reconcile_needed:
        reconcile_unlinked_receipts(db)
        reconcile_transfers(db)
        if matched_tx_rules:
            counterpart_updated = False
            for tx, matched_rule in matched_tx_rules:
                db.refresh(tx)
                if apply_counterpart_account_for_rule(db, tx, matched_rule):
                    counterpart_updated = True
            if counterpart_updated:
                db.commit()
                for tx in created:
                    db.refresh(tx)
                for tx in matched_existing:
                    db.refresh(tx)

    return len(created), skipped, created, matched_existing


def ingest_normalized_transactions(
    db: Session,
    account: Account,
    items: list[NormalizedTx] | tuple[NormalizedTx, ...],
    *,
    skip_llm: bool = False,
) -> tuple[int, int, list[Transaction]]:
    """Deduplicate, categorize, persist, and reconcile normalized transactions."""
    created_count, skipped_count, created, _ = ingest_normalized_transactions_detailed(
        db,
        account,
        items,
        skip_llm=skip_llm,
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
