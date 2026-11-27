"""Read-only FX rates router backed exclusively by cached exchange_rates."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ExchangeRate
from app.schemas import FxRateEntryRead, FxRatesRead

router = APIRouter(prefix="/fx", tags=["FX"])


def _normalize_target_currency(raw: str) -> str:
    """Normalize and validate an alphabetic ISO-style currency code."""
    target = raw.strip().upper()
    if len(target) < 3 or not target.isalpha():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="to_currency must be a non-empty alphabetic currency code (3-10 chars).",
        )
    return target


def _parse_requested_currencies(raw: str | None) -> list[str]:
    """Parse an optional comma-separated currency filter into deduplicated uppercase codes."""
    if not raw:
        return []
    seen: dict[str, None] = {}
    for token in raw.split(","):
        code = token.strip().upper()
        if code:
            seen.setdefault(code, None)
    return list(seen.keys())


def _query_cached_rates(
    db: Session,
    *,
    target: str,
    requested: list[str],
) -> dict[str, FxRateEntryRead]:
    """Query latest cached ExchangeRate rows per from_currency without any writes or HTTP calls."""
    stmt = select(ExchangeRate).where(ExchangeRate.to_currency == target)
    if requested:
        stmt = stmt.where(ExchangeRate.from_currency.in_(requested))
    stmt = stmt.order_by(
        ExchangeRate.rate_date.desc(),
        ExchangeRate.created_at.desc(),
        ExchangeRate.id.desc(),
    )

    rates: dict[str, FxRateEntryRead] = {}
    for row in db.scalars(stmt):
        code = row.from_currency.strip().upper()
        if code not in rates:
            rates[code] = FxRateEntryRead(
                rate=float(row.rate),
                rate_date=row.rate_date,
                source=row.source,
            )

    rates[target] = FxRateEntryRead(rate=1.0, rate_date=None, source="identity")
    return rates


@router.get("/rates", response_model=FxRatesRead)
def get_cached_fx_rates(
    to_currency: str = Query(default="NZD", min_length=3, max_length=10),
    currencies: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> FxRatesRead:
    """Return latest cached exchange rates per source currency without external FX calls."""
    target = _normalize_target_currency(to_currency)
    requested = _parse_requested_currencies(currencies)
    rates = _query_cached_rates(db, target=target, requested=requested)
    return FxRatesRead(to_currency=target, rates=rates)
