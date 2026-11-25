"""Cross-source reconciliation and external-note deduplication adapters."""

# ============================================================================
# 1. IMPORTS & CONSTANTS
# ============================================================================
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import partial
import json
import re
from typing import TYPE_CHECKING, Any, Callable, Optional, Protocol, Sequence

from sqlalchemy.orm import Session

from app.models import Account, CategorizationRule, Transaction
from app.services.ai_gateway import AiGateway
from app.services.fx_service import FxRateFetcher, get_exchange_rate
from app.services.ledger_entry import normalize_tags, parse_rule_tags
from app.services.transfer_reconciliation import unlink_transfer_pair

if TYPE_CHECKING:
    from app.services.ingestion_service import NormalizedTx

_STANDARD_NOTE_PREFIXES = ("akahu:", "wise:")
_OTHER_API_NOTE_PREFIXES = ("akahu:", "wise:", "btc:")
_DBS_REF_TOKEN_RE = re.compile(r"(?:^|[\s|;])(dbs:[^\s|;]+)")
_IN_CLAUSE_CHUNK_SIZE = 200
_GENERIC_DBS_MERCHANTS = frozenset(
    {
        "dbs transaction",
        "bill payment",
        "funds transfer",
        "fast payment",
        "unknown",
    }
)


# ============================================================================
# 2. DOMAIN CONTRACTS & PROTOCOL
# ============================================================================
@dataclass(frozen=True)
class DuplicateNoteResult:
    """Outcome of checking/refreshing an existing transaction matched by external note."""

    handled: bool
    refreshed: bool = False


@dataclass(frozen=True)
class CrossSourceMatchResult:
    """Outcome of reconciling an incoming item against an existing cross-source transaction."""

    matched: bool
    enriched: bool = False
    matched_tx: Optional[Transaction] = None
    matched_rules: tuple[tuple[Transaction, CategorizationRule], ...] = ()


class CrossSourceReconciler(Protocol):
    """Seam for provider-specific external-note extraction, duplicate refresh, and cross-source matching."""

    def extract_note_keys(self, notes: str) -> tuple[str, ...]:
        """Extract canonical external-id keys belonging to this adapter from a notes string."""
        ...

    def build_note_lookup_clauses(self, notes_column: Any, incoming_keys: Sequence[str]) -> list[Any]:
        """Build bounded SQLAlchemy filter clauses to look up incoming_keys on notes_column."""
        ...

    def on_duplicate_external_note(
        self,
        db: Session,
        account: Account,
        existing_tx: Transaction,
        item: NormalizedTx,
        *,
        exchange_rate_fn: Callable[..., Any],
    ) -> DuplicateNoteResult:
        """Handle or refresh an existing transaction when an incoming external note key matches."""
        ...

    def reconcile_cross_source(
        self,
        db: Session,
        account: Account,
        item: NormalizedTx,
        window_existing_txs: list[Transaction],
        existing_by_note: dict[str, Transaction],
        *,
        category_resolver: Callable[..., Any],
        gateway: AiGateway | None = None,
    ) -> CrossSourceMatchResult:
        """Attempt cross-source fuzzy matching and enrichment against window_existing_txs."""
        ...


# ============================================================================
# 3. SHARED PURE & PERSISTENCE HELPERS
# ============================================================================
def _chunked_in_clauses(notes_column: Any, keys: Sequence[str]) -> list[Any]:
    return [
        notes_column.in_(keys[idx : idx + _IN_CLAUSE_CHUNK_SIZE])
        for idx in range(0, len(keys), _IN_CLAUSE_CHUNK_SIZE)
    ]


def _amount_decimal_key(amount: float | Decimal) -> Decimal:
    return Decimal(str(amount)).quantize(Decimal("0.00000001"))


def extract_external_note_keys(
    notes: Optional[str],
    reconcilers: Sequence[CrossSourceReconciler],
) -> list[str]:
    """Aggregate unique canonical external-id keys from notes across reconcilers."""
    if not notes:
        return []
    stripped = notes.strip()
    if not stripped:
        return []
    keys: list[str] = []
    for reconciler in reconcilers:
        for key in reconciler.extract_note_keys(stripped):
            if key and key not in keys:
                keys.append(key)
    return keys


def refresh_existing_btc_transaction(
    db: Session,
    account: Account,
    item: NormalizedTx,
    existing_tx: Transaction,
    *,
    fetcher: Optional[FxRateFetcher] = None,
    exchange_rate_fn: Optional[Callable[..., Any]] = None,
) -> bool:
    """Refresh block-confirmed date and missing/stale FX rate on an existing BTC transaction."""
    reconcile_needed = existing_tx.linked_transaction_id is None
    date_changed = existing_tx.transaction_date != item.transaction_date
    unlinked = False
    if date_changed:
        existing_tx.transaction_date = item.transaction_date
        if existing_tx.linked_transaction_id is not None:
            unlink_transfer_pair(db, existing_tx, fetcher=fetcher)
            unlinked = True
        reconcile_needed = True

    needs_rate = (
        (not unlinked and date_changed)
        or existing_tx.exchange_rate is None
        or float(existing_tx.exchange_rate) < 1.0
    )
    if not needs_rate:
        return reconcile_needed

    tx_currency = (item.currency or account.currency or "BTC")[:10].upper()
    rate_fn = exchange_rate_fn or get_exchange_rate
    kwargs: dict[str, Any] = {
        "rate_date": item.transaction_date,
        "from_currency": tx_currency,
        "to_currency": "NZD",
    }
    if fetcher is not None:
        kwargs["fetcher"] = fetcher
    resolved = rate_fn(db, **kwargs)
    if resolved is not None and resolved[0] > 0:
        existing_tx.exchange_rate = round(float(resolved[0]), 8)
    return reconcile_needed


# ============================================================================
# 4. PROVIDER RECONCILER ADAPTERS
# ============================================================================
class StandardExternalNoteAdapter:
    """Exact external-id key adapter for standard bank feed prefixes (akahu:, wise:)."""

    def extract_note_keys(self, notes: str) -> tuple[str, ...]:
        stripped = (notes or "").strip()
        if stripped.startswith(_STANDARD_NOTE_PREFIXES):
            return (stripped,)
        return ()

    def build_note_lookup_clauses(self, notes_column: Any, incoming_keys: Sequence[str]) -> list[Any]:
        matched_keys = [k for k in incoming_keys if k.startswith(_STANDARD_NOTE_PREFIXES)]
        if not matched_keys:
            return []
        return _chunked_in_clauses(notes_column, matched_keys)

    def on_duplicate_external_note(
        self,
        db: Session,
        account: Account,
        existing_tx: Transaction,
        item: NormalizedTx,
        *,
        exchange_rate_fn: Callable[..., Any],
    ) -> DuplicateNoteResult:
        del db, account, existing_tx, item, exchange_rate_fn
        return DuplicateNoteResult(handled=False, refreshed=False)

    def reconcile_cross_source(
        self,
        db: Session,
        account: Account,
        item: NormalizedTx,
        window_existing_txs: list[Transaction],
        existing_by_note: dict[str, Transaction],
        *,
        category_resolver: Callable[..., Any],
        gateway: AiGateway | None = None,
    ) -> CrossSourceMatchResult:
        del db, account, item, window_existing_txs, existing_by_note, category_resolver, gateway
        return CrossSourceMatchResult(matched=False, enriched=False)


class BitcoinResyncReconciler:
    """Reconciler for btc: external keys supporting block-date confirmation and FX repair."""

    def extract_note_keys(self, notes: str) -> tuple[str, ...]:
        stripped = (notes or "").strip()
        if stripped.startswith("btc:"):
            return (stripped,)
        return ()

    def build_note_lookup_clauses(self, notes_column: Any, incoming_keys: Sequence[str]) -> list[Any]:
        btc_keys = [k for k in incoming_keys if k.startswith("btc:")]
        if not btc_keys:
            return []
        return _chunked_in_clauses(notes_column, btc_keys)

    def on_duplicate_external_note(
        self,
        db: Session,
        account: Account,
        existing_tx: Transaction,
        item: NormalizedTx,
        *,
        exchange_rate_fn: Callable[..., Any],
    ) -> DuplicateNoteResult:
        if not self.extract_note_keys(existing_tx.notes or ""):
            return DuplicateNoteResult(handled=False, refreshed=False)
        fetcher = (
            exchange_rate_fn.keywords.get("fetcher")
            if isinstance(exchange_rate_fn, partial)
            else None
        )
        refreshed = refresh_existing_btc_transaction(
            db,
            account,
            item,
            existing_tx,
            fetcher=fetcher,
            exchange_rate_fn=exchange_rate_fn,
        )
        return DuplicateNoteResult(handled=True, refreshed=refreshed)

    def reconcile_cross_source(
        self,
        db: Session,
        account: Account,
        item: NormalizedTx,
        window_existing_txs: list[Transaction],
        existing_by_note: dict[str, Transaction],
        *,
        category_resolver: Callable[..., Any],
        gateway: AiGateway | None = None,
    ) -> CrossSourceMatchResult:
        del db, account, item, window_existing_txs, existing_by_note, category_resolver, gateway
        return CrossSourceMatchResult(matched=False, enriched=False)


def _is_dbs_alert_notes(notes: Optional[str]) -> bool:
    if not notes:
        return False
    stripped = notes.strip()
    return (
        stripped == "dbs_email_alert"
        or "dbs_email_alert" in stripped
        or bool(_DBS_REF_TOKEN_RE.search(stripped))
    )


def _is_generic_dbs_merchant(merchant: Optional[str]) -> bool:
    cleaned = (merchant or "").strip().lower()
    if not cleaned:
        return True
    return cleaned.startswith("advice") or cleaned in _GENERIC_DBS_MERCHANTS


def _find_dbs_cross_source_match(
    item: NormalizedTx,
    item_currency: str,
    window_existing_txs: list[Transaction],
) -> Optional[Transaction]:
    incoming_is_dbs = _is_dbs_alert_notes(item.notes)
    item_amt_key = _amount_decimal_key(item.amount)
    candidates: list[tuple[int, int, Transaction]] = []

    for tx in window_existing_txs:
        if (tx.currency or "").strip().upper() != item_currency:
            continue
        if _amount_decimal_key(tx.amount) != item_amt_key:
            continue
        day_diff = abs((tx.transaction_date - item.transaction_date).days)
        if day_diff > 3:
            continue
        tx_is_dbs = _is_dbs_alert_notes(tx.notes)
        tx_is_other_api = bool(tx.notes and tx.notes.strip().startswith(_OTHER_API_NOTE_PREFIXES))
        if incoming_is_dbs and (tx_is_other_api or tx_is_dbs):
            continue
        if not incoming_is_dbs and not tx_is_dbs:
            continue
        candidates.append((day_diff, tx.id, tx))

    if not candidates:
        return None
    candidates.sort(key=lambda row: (row[0], row[1]))
    return candidates[0][2]


def _merge_dbs_match_notes(
    item: NormalizedTx,
    dbs_match: Transaction,
    existing_by_note: dict[str, Transaction],
    extractor: Callable[[str], tuple[str, ...]],
) -> None:
    if not item.notes:
        return
    if not dbs_match.notes:
        dbs_match.notes = item.notes
    elif item.notes not in dbs_match.notes:
        if item.notes.startswith("dbs:"):
            dbs_match.notes = f"{item.notes} | {dbs_match.notes}"
        else:
            dbs_match.notes = f"{dbs_match.notes} | {item.notes}"
    for key in extractor(dbs_match.notes or ""):
        existing_by_note[key] = dbs_match


def _append_raw_description(existing: Optional[str], incoming: Optional[str]) -> Optional[str]:
    if not incoming or incoming in (existing or ""):
        return existing
    return f"{existing} | {incoming}" if existing else incoming


def _enrich_matched_dbs_alert(
    item: NormalizedTx,
    dbs_match: Transaction,
    existing_by_note: dict[str, Transaction],
    extractor: Callable[[str], tuple[str, ...]],
    *,
    category_resolver: Callable[..., Any],
    gateway: AiGateway | None,
) -> tuple[tuple[Transaction, CategorizationRule], ...]:
    _merge_dbs_match_notes(item, dbs_match, existing_by_note, extractor)
    if not (_is_generic_dbs_merchant(dbs_match.merchant) and not _is_generic_dbs_merchant(item.merchant)):
        if item.category == "Transfers" and (dbs_match.category or "General") == "General":
            dbs_match.category = "Transfers"
        return ()

    merchant_name, tx_cat, rule_tags, rule_recurrence, matched_rule = category_resolver(
        item,
        gateway=gateway,
    )
    dbs_match.merchant = merchant_name
    dbs_match.raw_description = _append_raw_description(dbs_match.raw_description, item.raw_description)
    if (dbs_match.category or "General") == "General" or tx_cat == "Transfers":
        dbs_match.category = tx_cat[:50]
    if rule_recurrence and not dbs_match.recurrence:
        dbs_match.recurrence = rule_recurrence
    existing_item_tags = parse_rule_tags(dbs_match.tags)
    dbs_match.tags = json.dumps(normalize_tags([*existing_item_tags, *item.tags, *rule_tags]))
    if matched_rule is not None and matched_rule.loan_id is not None and dbs_match.loan_id is None:
        dbs_match.loan_id = matched_rule.loan_id
    if matched_rule is not None and matched_rule.counterpart_account_id is not None:
        return ((dbs_match, matched_rule),)
    return ()


def _enrich_matched_dbs_statement(item: NormalizedTx, dbs_match: Transaction) -> None:
    if _is_generic_dbs_merchant(dbs_match.merchant) and not _is_generic_dbs_merchant(item.merchant):
        dbs_match.merchant = item.merchant[:150]
    if item.raw_description and not _is_generic_dbs_merchant(item.raw_description):
        dbs_match.raw_description = _append_raw_description(
            dbs_match.raw_description,
            item.raw_description,
        )


class DbsCrossSourceReconciler:
    """Reconciler for dbs: keys and +/- 3-day DBS email-alert vs bank-statement matching."""

    def extract_note_keys(self, notes: str) -> tuple[str, ...]:
        stripped = (notes or "").strip()
        if not stripped:
            return ()
        keys: list[str] = []
        for match in _DBS_REF_TOKEN_RE.finditer(stripped):
            tok = match.group(1)
            if tok not in keys:
                keys.append(tok)
        return tuple(keys)

    def build_note_lookup_clauses(self, notes_column: Any, incoming_keys: Sequence[str]) -> list[Any]:
        dbs_keys = [k for k in incoming_keys if k.startswith("dbs:")]
        if not dbs_keys:
            return []
        return [notes_column.contains(key, autoescape=True) for key in dbs_keys]

    def on_duplicate_external_note(
        self,
        db: Session,
        account: Account,
        existing_tx: Transaction,
        item: NormalizedTx,
        *,
        exchange_rate_fn: Callable[..., Any],
    ) -> DuplicateNoteResult:
        del db, account, existing_tx, item, exchange_rate_fn
        return DuplicateNoteResult(handled=False, refreshed=False)

    def reconcile_cross_source(
        self,
        db: Session,
        account: Account,
        item: NormalizedTx,
        window_existing_txs: list[Transaction],
        existing_by_note: dict[str, Transaction],
        *,
        category_resolver: Callable[..., Any],
        gateway: AiGateway | None = None,
    ) -> CrossSourceMatchResult:
        del db
        item_currency = (item.currency or account.currency or "NZD")[:3].upper()
        dbs_match = _find_dbs_cross_source_match(item, item_currency, window_existing_txs)
        if dbs_match is None:
            return CrossSourceMatchResult(matched=False, enriched=False)

        matched_rules: tuple[tuple[Transaction, CategorizationRule], ...] = ()
        if _is_dbs_alert_notes(item.notes):
            matched_rules = _enrich_matched_dbs_alert(
                item,
                dbs_match,
                existing_by_note,
                self.extract_note_keys,
                category_resolver=category_resolver,
                gateway=gateway,
            )
        else:
            _enrich_matched_dbs_statement(item, dbs_match)
        return CrossSourceMatchResult(
            matched=True,
            enriched=True,
            matched_tx=dbs_match,
            matched_rules=matched_rules,
        )


def default_cross_source_reconcilers() -> tuple[CrossSourceReconciler, ...]:
    """Return the default ordered tuple of provider cross-source reconcilers."""
    return (
        StandardExternalNoteAdapter(),
        BitcoinResyncReconciler(),
        DbsCrossSourceReconciler(),
    )
