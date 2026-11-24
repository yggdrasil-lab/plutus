"""Shared reporting FX rate helper for ledger writes and unlinked transfer mutations."""

from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from app.services.fx_service import FxRateFetcher, get_exchange_rate


def resolve_nzd_exchange_rate(
    db: Session,
    currency: str,
    tx_date: date,
    *,
    fx_fetcher: FxRateFetcher | None = None,
) -> Optional[float]:
    """Return None for NZD or the resolved currency-to-NZD rate rounded to 8 decimals."""
    normalized = (currency or "NZD").strip().upper()
    if normalized == "NZD":
        return None
    resolved = get_exchange_rate(
        db,
        rate_date=tx_date,
        from_currency=normalized,
        to_currency="NZD",
        fetcher=fx_fetcher,
    )
    if resolved is not None and resolved[0] > 0:
        return round(float(resolved[0]), 8)
    return None
