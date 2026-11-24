"""Rule-based counterpart account attribution and mirror transfer leg lifecycle."""

from __future__ import annotations

import hashlib
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, CategorizationRule, Transaction
from app.services.fx_service import FxRateFetcher, get_exchange_rate
from app.services.transfer_reconciliation.constants import (
    CRYPTO_CURRENCIES,
    TRANSFER_CATEGORIES,
)
from app.services.transfer_reconciliation.primitives import link_transfer_pair


def apply_counterpart_account_for_rule(
    db: Session,
    tx: Transaction,
    rule: CategorizationRule,
    *,
    fetcher: Optional[FxRateFetcher] = None,
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

    is_transfer_rule = (rule.target_category or "").strip().lower() in TRANSFER_CATEGORIES
    if not is_transfer_rule or tx.linked_transaction_id is not None or float(tx.amount) == 0.0:
        return changed

    mirror_hash = _compute_mirror_hash(tx.raw_hash, rule.counterpart_account_id)
    existing_mirror = db.scalar(select(Transaction).where(Transaction.raw_hash == mirror_hash))
    if existing_mirror is not None:
        return _relink_existing_mirror(tx, existing_mirror) or changed

    _create_and_link_mirror(db, tx, counterpart_acc, mirror_hash, fetcher=fetcher)
    return True


def _compute_mirror_hash(raw_hash: str, counterpart_account_id: int) -> str:
    """Compute deterministic raw_hash for an auto-mirrored counterpart transfer leg."""
    return hashlib.sha256(f"mirror:{raw_hash}:{counterpart_account_id}".encode("utf-8")).hexdigest()


def _relink_existing_mirror(tx: Transaction, existing_mirror: Transaction) -> bool:
    """Re-link an unlinked mirror transaction back to its source leg when rules re-run."""
    if existing_mirror.linked_transaction_id is not None:
        return False
    link_transfer_pair(tx, existing_mirror)
    existing_mirror.category = "Transfers"
    if existing_mirror.counterpart_account_id != tx.account_id:
        existing_mirror.counterpart_account_id = tx.account_id
    if (tx.currency or "").strip().upper() == (existing_mirror.currency or "").strip().upper():
        tx.exchange_rate = 1.0
        existing_mirror.exchange_rate = 1.0
    return True


def _create_and_link_mirror(
    db: Session,
    tx: Transaction,
    counterpart_acc: Account,
    mirror_hash: str,
    *,
    fetcher: Optional[FxRateFetcher] = None,
) -> Transaction:
    """Persist a new mirror leg in counterpart_acc and link both legs symmetrically."""
    if tx.id is None:
        db.flush()
    mirror_currency, mirror_amount, mirror_fx = _compute_mirror_leg_values(
        db,
        tx,
        counterpart_acc,
        fetcher=fetcher,
    )
    mirror_tx = Transaction(
        transaction_date=tx.transaction_date,
        account_id=counterpart_acc.id,
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
    link_transfer_pair(tx, mirror_tx)
    return mirror_tx


def _compute_mirror_leg_values(
    db: Session,
    tx: Transaction,
    counterpart_acc: Account,
    *,
    fetcher: Optional[FxRateFetcher] = None,
) -> tuple[str, float, Optional[float]]:
    """Compute mirror currency, signed amount, and FX rates across same/cross-currency accounts."""
    tx_amt = float(tx.amount)
    src_currency = (tx.currency or "NZD").strip().upper()
    dst_currency = (counterpart_acc.currency or src_currency).strip().upper()
    if src_currency == dst_currency:
        tx.exchange_rate = 1.0
        return src_currency[:3], -tx_amt, 1.0

    resolved = get_exchange_rate(
        db,
        rate_date=tx.transaction_date,
        from_currency=src_currency,
        to_currency=dst_currency,
        fetcher=fetcher,
    )
    if resolved is not None and resolved[0] > 0:
        return _resolve_cross_currency_mirror(
            db,
            tx,
            src_currency,
            dst_currency,
            float(resolved[0]),
            fetcher=fetcher,
        )

    tx.exchange_rate = 1.0
    return src_currency[:3], -tx_amt, 1.0


def _resolve_cross_currency_mirror(
    db: Session,
    tx: Transaction,
    src_currency: str,
    dst_currency: str,
    conv_rate: float,
    *,
    fetcher: Optional[FxRateFetcher] = None,
) -> tuple[str, float, Optional[float]]:
    """Compute mirror currency, amount, and reporting FX rates for cross-currency mirrors."""
    tx_amt = float(tx.amount)
    mirror_currency = dst_currency[:3]
    decimals = 8 if dst_currency in CRYPTO_CURRENCIES else 2
    mirror_amount = round(-tx_amt * conv_rate, decimals)
    if tx_amt < 0:
        tx.exchange_rate = round(conv_rate, _fx_rate_precision(conv_rate))
        if dst_currency == "NZD":
            return mirror_currency, mirror_amount, 1.0
        dst_nzd = get_exchange_rate(
            db,
            rate_date=tx.transaction_date,
            from_currency=dst_currency,
            to_currency="NZD",
            fetcher=fetcher,
        )
        mirror_fx = round(float(dst_nzd[0]), 8) if dst_nzd and dst_nzd[0] > 0 else None
        return mirror_currency, mirror_amount, mirror_fx

    inv_rate = 1.0 / conv_rate
    mirror_fx = round(inv_rate, _fx_rate_precision(inv_rate))
    if src_currency == "NZD":
        tx.exchange_rate = 1.0
    elif tx.exchange_rate is None:
        src_nzd = get_exchange_rate(
            db,
            rate_date=tx.transaction_date,
            from_currency=src_currency,
            to_currency="NZD",
            fetcher=fetcher,
        )
        if src_nzd and src_nzd[0] > 0:
            tx.exchange_rate = round(float(src_nzd[0]), 8)
    return mirror_currency, mirror_amount, mirror_fx


def _fx_rate_precision(rate: float) -> int:
    """Return canonical decimal rounding precision for FX rates by magnitude."""
    return 8 if rate < 0.01 else 6 if rate < 1.0 else 4
