"""Compatibility facade for transfer matching, reconciliation, and crypto repair."""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.models import Transaction
from app.services.fx_repair import (
    repair_crypto_transfers_and_rates as _repair_crypto_transfers_and_rates,
)
from app.services.fx_service import FxRateFetcher, get_exchange_rate
from app.services.transfer_reconciliation import (
    CRYPTO_COUNTERPARTY_KEYWORDS,
    CRYPTO_CURRENCIES,
    HIGH_DENOM_FIAT,
    NON_TRANSFER_NOISE,
    SUBACCOUNT_TAG_RE,
    TRANSFER_KEYWORDS,
)
from app.services.transfer_reconciliation.matching import (
    match_transfer_pair as _match_transfer_pair,
    reconcile_transfers as _reconcile_transfers,
)

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
    """Evaluate all unlinked transfer candidates and persist matches in one commit."""
    return _reconcile_transfers(
        db,
        fetcher=fetcher,
        rate_lookup=get_exchange_rate,
    )


def repair_crypto_transfers_and_rates(
    db: Session,
    *,
    fetcher: Optional[FxRateFetcher] = None,
) -> int:
    """Repair inverted/fallback crypto FX rates and unlink mismatched crypto transfer legs."""
    return _repair_crypto_transfers_and_rates(
        db,
        fetcher=fetcher,
        rate_lookup=get_exchange_rate,
    )
