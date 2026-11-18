"""Historical and spot FX rate service with PostgreSQL caching (Wise, Frankfurter, CoinGecko)."""

from datetime import date
import logging
from typing import Optional, Protocol

import httpx
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ExchangeRate

logger = logging.getLogger(__name__)

CRYPTO_COINGECKO_IDS: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "USDC": "usd-coin",
    "USDT": "tether",
}

WISE_RATES_URL = "https://api.wise.com/v1/rates"
FRANKFURTER_BASE_URL = "https://api.frankfurter.dev/v1"
COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"
COINBASE_PRICES_URL = "https://api.coinbase.com/v2/prices"


class FxRateFetcher(Protocol):
    """Protocol for fetching an exchange rate from an external provider."""

    def fetch_rate(
        self,
        *,
        rate_date: date,
        from_currency: str,
        to_currency: str,
    ) -> Optional[tuple[float, str]]:
        """Return (rate, source) or None if unavailable."""
        ...


class HttpFxRateFetcher:
    """Default HTTP FX rate fetcher routing fiat via Wise/Frankfurter and crypto via CoinGecko."""

    def __init__(self, wise_api_token: str = "", timeout: float = 8.0) -> None:
        self._wise_api_token = wise_api_token
        self._timeout = timeout

    def fetch_rate(
        self,
        *,
        rate_date: date,
        from_currency: str,
        to_currency: str,
    ) -> Optional[tuple[float, str]]:
        src = from_currency.upper()
        dst = to_currency.upper()

        if src in CRYPTO_COINGECKO_IDS:
            if rate_date < date.today():
                cb_rate = self._fetch_coinbase(rate_date=rate_date, from_currency=src, to_currency=dst)
                if cb_rate is not None:
                    return cb_rate, "coinbase"
                if (date.today() - rate_date).days <= 365:
                    return self._fetch_coingecko(rate_date=rate_date, coin_id=CRYPTO_COINGECKO_IDS[src], to_currency=dst)
                return None

            cg_rate = self._fetch_coingecko(rate_date=rate_date, coin_id=CRYPTO_COINGECKO_IDS[src], to_currency=dst)
            if cg_rate is not None:
                return cg_rate
            cb_rate = self._fetch_coinbase(rate_date=rate_date, from_currency=src, to_currency=dst)
            if cb_rate is not None:
                return cb_rate, "coinbase"
            return None

        if dst in CRYPTO_COINGECKO_IDS:
            inverse = self.fetch_rate(rate_date=rate_date, from_currency=dst, to_currency=src)
            if inverse is not None and inverse[0] > 0:
                return 1.0 / float(inverse[0]), f"{inverse[1]}_inverse"
            return None

        if self._wise_api_token:
            wise_rate = self._fetch_wise(rate_date=rate_date, from_currency=src, to_currency=dst)
            if wise_rate is not None:
                return wise_rate, "wise"

        frank_rate = self._fetch_frankfurter(rate_date=rate_date, from_currency=src, to_currency=dst)
        if frank_rate is not None:
            return frank_rate, "frankfurter"

        return None

    def _fetch_coinbase(self, *, rate_date: date, from_currency: str, to_currency: str) -> Optional[float]:
        """Fetch historical or spot crypto exchange rate from Coinbase public prices API."""
        url = f"{COINBASE_PRICES_URL}/{from_currency}-{to_currency}/spot"
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.get(url, params={"date": rate_date.isoformat()})
            if resp.status_code == 200:
                val = ((resp.json().get("data") or {}).get("amount"))
                if val is not None and float(val) > 0:
                    return float(val)
        except Exception as exc:
            logger.debug("Coinbase FX lookup failed for %s->%s (%s): %s", from_currency, to_currency, rate_date, exc)
        return None

    def _fetch_wise(self, *, rate_date: date, from_currency: str, to_currency: str) -> Optional[float]:
        params = {"source": from_currency, "target": to_currency}
        if rate_date < date.today():
            params["time"] = f"{rate_date.isoformat()}T12:00:00Z"
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.get(
                    WISE_RATES_URL,
                    params=params,
                    headers={"Authorization": f"Bearer {self._wise_api_token}"},
                )
            if resp.status_code == 200:
                items = resp.json()
                if isinstance(items, list) and items:
                    val = items[0].get("rate")
                    if val is not None and float(val) > 0:
                        return float(val)
        except Exception as exc:
            logger.debug("Wise FX rate lookup failed for %s->%s (%s): %s", from_currency, to_currency, rate_date, exc)
        return None

    def _fetch_frankfurter(self, *, rate_date: date, from_currency: str, to_currency: str) -> Optional[float]:
        url = f"{FRANKFURTER_BASE_URL}/{rate_date.isoformat()}"
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.get(url, params={"base": from_currency, "symbols": to_currency})
            if resp.status_code == 200:
                rates = resp.json().get("rates") or {}
                val = rates.get(to_currency)
                if val is not None and float(val) > 0:
                    return float(val)
        except Exception as exc:
            logger.debug("Frankfurter FX lookup failed for %s->%s (%s): %s", from_currency, to_currency, rate_date, exc)
        return None

    def _fetch_coingecko(
        self,
        *,
        rate_date: date,
        coin_id: str,
        to_currency: str,
    ) -> Optional[tuple[float, str]]:
        target_lower = to_currency.lower()
        try:
            with httpx.Client(timeout=self._timeout) as client:
                if rate_date >= date.today():
                    resp = client.get(
                        f"{COINGECKO_BASE_URL}/simple/price",
                        params={"ids": coin_id, "vs_currencies": target_lower},
                    )
                    if resp.status_code == 200:
                        val = (resp.json().get(coin_id) or {}).get(target_lower)
                        if val is not None and float(val) > 0:
                            return float(val), "coingecko"
                elif (date.today() - rate_date).days <= 365:
                    resp = client.get(
                        f"{COINGECKO_BASE_URL}/coins/{coin_id}/history",
                        params={"date": rate_date.strftime("%d-%m-%Y"), "localization": "false"},
                    )
                    if resp.status_code == 200:
                        prices = ((resp.json().get("market_data") or {}).get("current_price")) or {}
                        val = prices.get(target_lower)
                        if val is not None and float(val) > 0:
                            return float(val), "coingecko"
        except Exception as exc:
            logger.debug("CoinGecko FX lookup failed for %s->%s (%s): %s", coin_id, to_currency, rate_date, exc)
        return None


def to_nzd_amount(
    amount: float,
    currency: Optional[str],
    exchange_rate: Optional[float],
) -> float:
    """Convert an expense or income amount to absolute NZD using stored FX only for foreign currencies."""
    abs_amt = abs(float(amount))
    code = (currency or "NZD").strip().upper()
    if code == "NZD":
        return abs_amt
    rate = float(exchange_rate) if (exchange_rate is not None and float(exchange_rate) > 0) else 1.0
    return abs_amt * rate


def cache_exchange_rate(
    db: Session,
    *,
    rate_date: date,
    from_currency: str,
    to_currency: str,
    rate: float,
    source: str,
) -> ExchangeRate:
    """Insert or update a cached exchange rate row."""
    src = from_currency.strip().upper()[:10]
    dst = to_currency.strip().upper()[:10]
    existing = db.scalar(
        select(ExchangeRate).where(
            ExchangeRate.rate_date == rate_date,
            ExchangeRate.from_currency == src,
            ExchangeRate.to_currency == dst,
        )
    )
    if existing:
        existing.rate = rate
        existing.source = source[:30]
        db.commit()
        db.refresh(existing)
        return existing

    row = ExchangeRate(
        rate_date=rate_date,
        from_currency=src,
        to_currency=dst,
        rate=rate,
        source=source[:30],
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(ExchangeRate).where(
                ExchangeRate.rate_date == rate_date,
                ExchangeRate.from_currency == src,
                ExchangeRate.to_currency == dst,
            )
        )
        if existing is None:
            raise
        existing.rate = rate
        existing.source = source[:30]
        db.commit()
        db.refresh(existing)
        return existing
    db.refresh(row)
    return row


def has_exact_exchange_rate(
    db: Session,
    *,
    rate_date: date,
    from_currency: str,
    to_currency: str = "NZD",
) -> bool:
    """Return True when an exact-date rate exists in cache for (rate_date, from_currency, to_currency)."""
    src = (from_currency or "NZD").strip().upper()[:10]
    dst = (to_currency or "NZD").strip().upper()[:10]
    if src == dst:
        return True
    return (
        db.scalar(
            select(ExchangeRate.id).where(
                ExchangeRate.rate_date == rate_date,
                ExchangeRate.from_currency == src,
                ExchangeRate.to_currency == dst,
            )
        )
        is not None
    )


def _resolve_exact_date_rate(
    db: Session,
    *,
    rate_date: date,
    src: str,
    dst: str,
    fetcher: FxRateFetcher,
) -> Optional[tuple[float, str]]:
    """Resolve an exchange rate strictly for `rate_date` using cache or external fetcher."""
    if src == dst:
        return 1.0, "identity"

    cached = db.scalar(
        select(ExchangeRate).where(
            ExchangeRate.rate_date == rate_date,
            ExchangeRate.from_currency == src,
            ExchangeRate.to_currency == dst,
        )
    )
    if cached:
        return float(cached.rate), cached.source

    exact_inverse = db.scalar(
        select(ExchangeRate).where(
            ExchangeRate.rate_date == rate_date,
            ExchangeRate.from_currency == dst,
            ExchangeRate.to_currency == src,
        )
    )
    if exact_inverse and float(exact_inverse.rate) > 0:
        return 1.0 / float(exact_inverse.rate), f"{exact_inverse.source}_inverse"

    if dst in CRYPTO_COINGECKO_IDS and src not in CRYPTO_COINGECKO_IDS:
        fetched_fwd = fetcher.fetch_rate(rate_date=rate_date, from_currency=dst, to_currency=src)
        if fetched_fwd is not None and fetched_fwd[0] > 0:
            rate_val, source = fetched_fwd
            cache_exchange_rate(
                db,
                rate_date=rate_date,
                from_currency=dst,
                to_currency=src,
                rate=rate_val,
                source=source,
            )
            return 1.0 / float(rate_val), f"{source}_inverse"

    fetched = fetcher.fetch_rate(rate_date=rate_date, from_currency=src, to_currency=dst)
    if fetched is not None:
        rate_val, source = fetched
        cache_exchange_rate(
            db,
            rate_date=rate_date,
            from_currency=src,
            to_currency=dst,
            rate=rate_val,
            source=source,
        )
        return rate_val, source

    return None


def get_exchange_rate(
    db: Session,
    *,
    rate_date: date,
    from_currency: str,
    to_currency: str = "NZD",
    fetcher: Optional[FxRateFetcher] = None,
) -> Optional[tuple[float, str]]:
    """Resolve an exchange rate from exact-date cache, external providers, NZD triangulation, or latest fallback."""
    src = (from_currency or "NZD").strip().upper()[:10]
    dst = (to_currency or "NZD").strip().upper()[:10]
    if src == dst:
        return 1.0, "identity"

    resolved_fetcher = fetcher or HttpFxRateFetcher(wise_api_token=settings.wise_api_token)
    exact = _resolve_exact_date_rate(
        db,
        rate_date=rate_date,
        src=src,
        dst=dst,
        fetcher=resolved_fetcher,
    )
    if exact is not None:
        return exact

    if src != "NZD" and dst != "NZD":
        src_to_nzd = _resolve_exact_date_rate(
            db,
            rate_date=rate_date,
            src=src,
            dst="NZD",
            fetcher=resolved_fetcher,
        )
        dst_to_nzd = _resolve_exact_date_rate(
            db,
            rate_date=rate_date,
            src=dst,
            dst="NZD",
            fetcher=resolved_fetcher,
        )
        if src_to_nzd is not None and dst_to_nzd is not None and dst_to_nzd[0] > 0:
            return src_to_nzd[0] / dst_to_nzd[0], f"{src_to_nzd[1]}/{dst_to_nzd[1]}"

    latest_fallback = db.scalar(
        select(ExchangeRate)
        .where(
            ExchangeRate.from_currency == src,
            ExchangeRate.to_currency == dst,
        )
        .order_by(desc(ExchangeRate.rate_date))
        .limit(1)
    )
    if latest_fallback:
        return float(latest_fallback.rate), latest_fallback.source

    inverse_fallback = db.scalar(
        select(ExchangeRate)
        .where(
            ExchangeRate.from_currency == dst,
            ExchangeRate.to_currency == src,
        )
        .order_by(desc(ExchangeRate.rate_date))
        .limit(1)
    )
    if inverse_fallback and float(inverse_fallback.rate) > 0:
        return 1.0 / float(inverse_fallback.rate), f"{inverse_fallback.source}_inverse"

    return None
