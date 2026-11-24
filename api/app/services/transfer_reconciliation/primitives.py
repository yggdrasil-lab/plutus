"""Canonical transfer pair link, unlink, and reporting FX restoration primitives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Transaction
from app.services.fx_service import FxRateFetcher, get_exchange_rate


@dataclass(frozen=True)
class UnlinkTransferResult:
    """Outcome of unlinking a transfer transaction and restoring reporting FX state."""

    unlinked: bool
    partner_id: Optional[int]


def resolve_unlinked_reporting_rate(
    db: Session,
    leg: Transaction,
    *,
    fetcher: Optional[FxRateFetcher] = None,
) -> Optional[float]:
    """Return None for NZD or resolved currency-to-NZD rate rounded to 8 decimals."""
    normalized = (leg.currency or "NZD").strip().upper()
    if normalized == "NZD":
        return None
    resolved = get_exchange_rate(
        db,
        rate_date=leg.transaction_date,
        from_currency=normalized,
        to_currency="NZD",
        fetcher=fetcher,
    )
    if resolved is not None and resolved[0] > 0:
        return round(float(resolved[0]), 8)
    return None


def link_transfer_pair(
    leg_a: Transaction,
    leg_b: Transaction,
    *,
    promote_general_category: bool = False,
) -> None:
    """Symmetrically link two persisted transfer legs, failing loudly on invalid IDs."""
    if leg_a.id is None or leg_b.id is None:
        raise ValueError("Both transfer legs must be persisted with non-null IDs before linking.")
    if leg_a.id == leg_b.id:
        raise ValueError("Cannot link a transaction to itself.")

    leg_a.linked_transaction_id = leg_b.id
    leg_b.linked_transaction_id = leg_a.id
    if not promote_general_category:
        return
    if leg_a.category == "General":
        leg_a.category = "Transfers"
    if leg_b.category == "General":
        leg_b.category = "Transfers"


def unlink_transfer_pair(
    db: Session,
    tx: Transaction,
    *,
    fetcher: Optional[FxRateFetcher] = None,
    restore_self_when_unlinked: bool = False,
) -> UnlinkTransferResult:
    """Unlink a transaction and its partner (if present), clearing fees and restoring NZD FX."""
    if tx.linked_transaction_id is not None:
        partner = db.get(Transaction, tx.linked_transaction_id)
        if partner is not None:
            _clear_unlinked_leg(db, partner, fetcher=fetcher)
        _clear_unlinked_leg(db, tx, fetcher=fetcher)
        return UnlinkTransferResult(
            unlinked=True,
            partner_id=partner.id if partner is not None else None,
        )

    if restore_self_when_unlinked:
        _clear_unlinked_leg(db, tx, fetcher=fetcher)
    return UnlinkTransferResult(unlinked=False, partner_id=None)


def _clear_unlinked_leg(
    db: Session,
    leg: Transaction,
    *,
    fetcher: Optional[FxRateFetcher] = None,
) -> None:
    """Reset transfer link, transfer fee, and reporting FX rate on one unlinked leg."""
    leg.linked_transaction_id = None
    leg.fee_amount = None
    leg.exchange_rate = resolve_unlinked_reporting_rate(db, leg, fetcher=fetcher)
