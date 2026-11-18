"""Unit tests for historical/spot FX rate caching and Wise/Frankfurter/CoinGecko conversion."""

from datetime import date
from pathlib import Path
import sys
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base
from app.models import Account, ExchangeRate
from app.services.fx_service import cache_exchange_rate, get_exchange_rate
from app.services.ingestion_service import upsert_account_snapshot

TEST_DB_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class StubFxFetcher:
    """In-memory stub fetcher tracking call counts."""

    def __init__(self, mapping: dict[tuple[date, str, str], tuple[float, str]]):
        self.mapping = mapping
        self.calls: list[tuple[date, str, str]] = []

    def fetch_rate(self, *, rate_date: date, from_currency: str, to_currency: str):
        key = (rate_date, from_currency.upper(), to_currency.upper())
        self.calls.append(key)
        return self.mapping.get(key)


class TestFxService(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_caches_fetched_rate_and_hits_db_on_repeat(self):
        d = date(2026, 11, 15)
        fetcher = StubFxFetcher({(d, "SGD", "NZD"): (1.28, "wise")})

        first = get_exchange_rate(self.db, rate_date=d, from_currency="SGD", to_currency="NZD", fetcher=fetcher)
        self.assertEqual(first, (1.28, "wise"))
        self.assertEqual(len(fetcher.calls), 1)

        second = get_exchange_rate(self.db, rate_date=d, from_currency="SGD", to_currency="NZD", fetcher=fetcher)
        self.assertEqual(second, (1.28, "wise"))
        # Second lookup must hit PostgreSQL/SQLite cache without calling external fetcher
        self.assertEqual(len(fetcher.calls), 1)

    def test_crypto_rate_and_fallback_to_latest_cached(self):
        d1 = date(2026, 11, 10)
        d2 = date(2026, 11, 15)
        cache_exchange_rate(self.db, rate_date=d1, from_currency="BTC", to_currency="NZD", rate=152000.0, source="coingecko")

        empty_fetcher = StubFxFetcher({})
        resolved = get_exchange_rate(self.db, rate_date=d2, from_currency="BTC", to_currency="NZD", fetcher=empty_fetcher)
        self.assertEqual(resolved, (152000.0, "coingecko"))

    def test_snapshot_converts_wise_foreign_balance_to_nzd(self):
        d = date(2026, 11, 15)
        acc = Account(
            name="Wise SGD",
            account_type="checking",
            tier="cash",
            institution="Wise",
            currency="SGD",
        )
        self.db.add(acc)
        self.db.commit()
        self.db.refresh(acc)

        cache_exchange_rate(self.db, rate_date=d, from_currency="SGD", to_currency="NZD", rate=1.25, source="wise")

        snap = upsert_account_snapshot(
            self.db,
            account=acc,
            snapshot_date=d,
            balance=1000.0,
            currency="SGD",
            source="wise_sync",
        )
        self.assertAlmostEqual(float(snap.exchange_rate), 1.25, places=4)
        self.assertAlmostEqual(float(snap.balance_nzd), 1250.0, places=2)

    def test_historical_crypto_uses_coinbase_beyond_365_days_and_caches_canonical_direction(self):
        from unittest.mock import MagicMock, patch
        from app.services.fx_service import HttpFxRateFetcher

        d_hist = date(2024, 11, 13)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": {"amount": "148616.57", "base": "BTC", "currency": "NZD"}}

        with patch("httpx.Client.get", return_value=resp) as mock_get:
            # Query NZD -> BTC (reverse direction used during transfer reconciliation)
            inv = get_exchange_rate(
                self.db,
                rate_date=d_hist,
                from_currency="NZD",
                to_currency="BTC",
                fetcher=HttpFxRateFetcher(),
            )
            self.assertEqual(mock_get.call_count, 1)

        self.assertIsNotNone(inv)
        self.assertAlmostEqual(inv[0], 1.0 / 148616.57, places=10)
        self.assertEqual(inv[1], "coinbase_inverse")

        # Forward BTC -> NZD should now hit cache without calling HTTP again
        cached_row = self.db.query(ExchangeRate).filter(
            ExchangeRate.rate_date == d_hist,
            ExchangeRate.from_currency == "BTC",
            ExchangeRate.to_currency == "NZD",
        ).one()
        self.assertAlmostEqual(float(cached_row.rate), 148616.57, places=2)
        fwd = get_exchange_rate(self.db, rate_date=d_hist, from_currency="BTC", to_currency="NZD")
        self.assertAlmostEqual(fwd[0], 148616.57, places=2)


if __name__ == "__main__":
    unittest.main()
