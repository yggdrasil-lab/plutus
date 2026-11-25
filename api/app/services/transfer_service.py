"""Compatibility facade for transfer matching, reconciliation, and crypto repair."""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Transaction
from app.services.fx_service import FxRateFetcher, get_exchange_rate, has_exact_exchange_rate
from app.services.transfer_reconciliation import (
    CRYPTO_COUNTERPARTY_KEYWORDS,
    CRYPTO_CURRENCIES,
    HIGH_DENOM_FIAT,
    NON_TRANSFER_NOISE,
    SUBACCOUNT_TAG_RE,
    TRANSFER_KEYWORDS,
    extract_fiat_to_crypto_fee,
    unlink_transfer_pair,
)
from app.services.transfer_reconciliation.matching import (
    match_transfer_pair as _match_transfer_pair,
    reconcile_transfers as _reconcile_transfers,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CRYPTO_COUNTERPARTY_KEYWORDS",
    "CRYPTO_CURRENCIES",
    "HIGH_DENOM_FIAT",
    "NON_TRANSFER_NOISE",
    "SUBACCOUNT_TAG_RE",
    "TRANSFER_KEYWORDS",
    "match_transfer_pair",
    "reconcile_transfers",
    "repair_crypto_transfers_and_rates",
]


def match_transfer_pair(
    tx: Transaction,
    db: Session,
    *,
    fetcher: Optional[FxRateFetcher] = None,
) -> Optional[Transaction]:
    """Find and link the matching counter-leg for an account transfer."""
    return _match_transfer_pair(
        tx,
        db,
        fetcher=fetcher,
        rate_lookup=get_exchange_rate,
    )


def reconcile_transfers(
    db: Session,
    *,
    fetcher: Optional[FxRateFetcher] = None,
) -> int:
    """Evaluate all unlinked transfer candidates and pair matching legs across accounts."""
    return _reconcile_transfers(
        db,
        fetcher=fetcher,
        rate_lookup=get_exchange_rate,
    )


def _sync_crypto_reporting_rate(
    db: Session,
    tx: Transaction,
    *,
    fetcher: Optional[FxRateFetcher] = None,
) -> int:
    """Refresh missing, inverted, or stale crypto-to-NZD reporting rate on a single leg."""
    code = (tx.currency or "").strip().upper()
    needs_exact_check = (
        not has_exact_exchange_rate(
            db, rate_date=tx.transaction_date, from_currency=code, to_currency="NZD"
        )
        or tx.exchange_rate is None
        or float(tx.exchange_rate) < 1.0
    )
    resolved_nzd = get_exchange_rate(
        db,
        rate_date=tx.transaction_date,
        from_currency=code,
        to_currency="NZD",
        fetcher=fetcher,
    )
    if resolved_nzd is None or resolved_nzd[0] <= 0:
        return 0
    target_rate = round(float(resolved_nzd[0]), 8)
    cur_rate = float(tx.exchange_rate or 0.0)
    needs_update = (
        cur_rate <= 0
        or cur_rate < 1.0
        or abs(cur_rate - target_rate) / target_rate > 0.001
        or needs_exact_check
    )
    if needs_update and abs(cur_rate - target_rate) > 1e-8:
        tx.exchange_rate = target_rate
        return 1
    return 0


def _should_unlink_crypto_leg(
    db: Session,
    tx: Transaction,
    partner: Transaction,
    outflow: Transaction,
    inflow: Transaction,
    *,
    fetcher: Optional[FxRateFetcher] = None,
) -> bool:
    """Return True if a linked crypto transfer violates market spread or counterparty guards."""
    out_code = (outflow.currency or "").strip().upper()
    in_code = (inflow.currency or "").strip().upper()
    out_amt = abs(float(outflow.amount))
    fee = float(outflow.fee_amount or 0.0)
    net_out = out_amt - fee if out_amt > fee else out_amt
    in_amt = float(inflow.amount)
    implied_rate = (in_amt / net_out) if (net_out > 0 and in_amt > 0) else 0.0
    market = get_exchange_rate(
        db,
        rate_date=tx.transaction_date,
        from_currency=out_code,
        to_currency=in_code,
        fetcher=fetcher,
    )
    if market is not None and market[0] > 0 and implied_rate > 0:
        return abs(implied_rate - market[0]) / market[0] > 0.12
    if out_code != "NZD" and in_code != "NZD":
        desc_partner = f"{partner.merchant or ''} {partner.raw_description or ''}"
        return CRYPTO_COUNTERPARTY_KEYWORDS.search(desc_partner) is None
    return False


def _backfill_nzd_crypto_fee(
    db: Session,
    outflow: Transaction,
    inflow: Transaction,
    *,
    fetcher: Optional[FxRateFetcher] = None,
) -> int:
    """Backfill implied conversion spread fee and effective NZD/crypto rate on paired NZD leg."""
    out_amt = abs(float(outflow.amount))
    fee = float(outflow.fee_amount or 0.0)
    net_out = out_amt - fee if out_amt > fee else out_amt
    in_amt = float(inflow.amount)
    if net_out <= 0 or in_amt <= 0:
        return 0
    updates = 0
    extracted_fee = extract_fiat_to_crypto_fee(
        db,
        outflow=outflow,
        inflow=inflow,
        fetcher=fetcher,
        rate_lookup=get_exchange_rate,
    )
    if extracted_fee is not None:
        outflow.fee_amount = extracted_fee
        net_out = out_amt - extracted_fee if out_amt > extracted_fee else out_amt
        updates += 1
    expected_nzd_per_crypto = round(net_out / in_amt, 8)
    if abs(float(outflow.exchange_rate or 0.0) - expected_nzd_per_crypto) > 1e-4:
        outflow.exchange_rate = expected_nzd_per_crypto
        updates += 1
    return updates


def _repair_linked_crypto_leg(
    db: Session,
    tx: Transaction,
    *,
    fetcher: Optional[FxRateFetcher] = None,
) -> int:
    """Repair or unlink one linked crypto transfer leg using canonical unlink primitives."""
    if tx.linked_transaction_id is None:
        return 0
    partner = db.get(Transaction, tx.linked_transaction_id)
    if partner is None:
        unlink_transfer_pair(db, tx, fetcher=fetcher)
        return 1
    outflow = partner if partner.amount < 0 else tx
    inflow = tx if tx.amount > 0 else partner
    out_code = (outflow.currency or "").strip().upper()
    in_code = (inflow.currency or "").strip().upper()
    if out_code == in_code:
        return 0
    if _should_unlink_crypto_leg(db, tx, partner, outflow, inflow, fetcher=fetcher):
        unlink_transfer_pair(db, tx, fetcher=fetcher)
        return 1
    if out_code == "NZD" and in_code in CRYPTO_CURRENCIES:
        return _backfill_nzd_crypto_fee(db, outflow, inflow, fetcher=fetcher)
    return 0


def repair_crypto_transfers_and_rates(
    db: Session,
    *,
    fetcher: Optional[FxRateFetcher] = None,
) -> int:
    """Repair inverted/fallback crypto FX rates and unlink mismatched crypto transfer legs."""
    crypto_rows = [
        tx
        for tx in db.scalars(select(Transaction).order_by(Transaction.transaction_date.asc())).all()
        if (tx.currency or "").strip().upper() in CRYPTO_CURRENCIES
    ]
    if not crypto_rows:
        return 0

    repaired = 0
    for tx in crypto_rows:
        repaired += _sync_crypto_reporting_rate(db, tx, fetcher=fetcher)
        repaired += _repair_linked_crypto_leg(db, tx, fetcher=fetcher)

    if repaired > 0:
        db.commit()
    return repaired
