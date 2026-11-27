"""Pure credit telemetry calculations and fail-loud NZD FX resolution for revolving credit accounts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from app.schemas.accounts import REVOLVING_CREDIT_ACCOUNT_TYPES
from app.services.fx_service import get_exchange_rate


class CreditFxResolutionError(ValueError):
    """Raised when a non-NZD revolving credit account has a credit limit without a resolvable NZD FX rate."""


@dataclass(frozen=True)
class AccountCreditTelemetry:
    """Immutable derived credit limit, owed, available, and utilization metrics for one account."""

    credit_limit_nzd: float
    credit_owed_native: float
    credit_owed_nzd: float
    available_credit_native: float
    available_credit_nzd: float
    credit_utilization_pct: float


class _DbCacheOnlyFxFetcher:
    """No-op FX fetcher preventing external HTTP calls during account or runway reads."""

    def fetch_rate(
        self,
        *,
        rate_date: date,
        from_currency: str,
        to_currency: str,
    ) -> Optional[tuple[float, str]]:
        return None


_DB_ONLY_FX_FETCHER = _DbCacheOnlyFxFetcher()


def is_revolving_credit_with_limit(
    account_type: Optional[str],
    credit_limit: Optional[float],
) -> bool:
    """Return True when an account is a revolving credit type with a positive credit limit."""
    return (
        (account_type or "").strip().lower() in REVOLVING_CREDIT_ACCOUNT_TYPES
        and credit_limit is not None
        and float(credit_limit) > 0.0
    )


def resolve_credit_fx_rate(
    db: Session,
    *,
    currency: Optional[str],
    rate_date: date,
    account_name: str,
    snapshot_exchange_rate: Optional[float] = None,
    rate_cache: Optional[dict[tuple[str, date], Optional[float]]] = None,
) -> float:
    """Resolve NZD FX rate for a credit account from DB cache or non-default snapshot rate, failing loud if missing."""
    code = (currency or "NZD").strip().upper()
    if code == "NZD":
        return 1.0

    cache_key = (code, rate_date)
    if rate_cache is not None and cache_key in rate_cache:
        cached = rate_cache[cache_key]
    else:
        resolved = get_exchange_rate(
            db,
            rate_date=rate_date,
            from_currency=code,
            to_currency="NZD",
            fetcher=_DB_ONLY_FX_FETCHER,
        )
        cached = float(resolved[0]) if (resolved is not None and float(resolved[0]) > 0.0) else None
        if rate_cache is not None:
            rate_cache[cache_key] = cached

    if cached is not None:
        return cached

    if snapshot_exchange_rate is not None:
        snap_rate = float(snapshot_exchange_rate)
        if snap_rate > 0.0 and snap_rate != 1.0:
            return snap_rate

    raise CreditFxResolutionError(
        f"Cannot resolve {code}->NZD FX rate for credit account '{account_name}'. "
        "Cache an exchange rate or record a non-default snapshot FX rate."
    )


def compute_account_credit_telemetry(
    *,
    account_type: Optional[str],
    credit_limit: Optional[float],
    latest_balance: Optional[float],
    fx_rate: float,
) -> Optional[AccountCreditTelemetry]:
    """Compute immutable credit limit, owed, available credit, and utilization metrics."""
    if not is_revolving_credit_with_limit(account_type, credit_limit):
        return None
    if fx_rate <= 0.0:
        raise CreditFxResolutionError(f"FX rate must be positive, got {fx_rate}.")

    limit_native = float(credit_limit)
    limit_nzd = round(limit_native * fx_rate, 2)
    owed_native = round(max(0.0, -float(latest_balance)), 2) if latest_balance is not None else 0.0
    owed_nzd = round(owed_native * fx_rate, 2)
    available_native = round(max(0.0, limit_native - owed_native), 2)
    available_nzd = round(max(0.0, limit_nzd - owed_nzd), 2)
    utilization_pct = round((owed_native / limit_native) * 100.0, 2)
    return AccountCreditTelemetry(
        credit_limit_nzd=limit_nzd,
        credit_owed_native=owed_native,
        credit_owed_nzd=owed_nzd,
        available_credit_native=available_native,
        available_credit_nzd=available_nzd,
        credit_utilization_pct=utilization_pct,
    )
