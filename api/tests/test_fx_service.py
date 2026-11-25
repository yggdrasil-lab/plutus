"""Unit tests for historical/spot FX rate caching, session reuse, and flush-safe savepoints."""

from datetime import date
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base
from app.models import Account, ExchangeRate, Transaction
from app.services.fx_service import (
    HttpFxRateFetcher,
    _find_cached_rate,
    cache_exchange_rate,
    get_exchange_rate,
    resolve_reporting_rate,
)
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
        from unittest.mock import MagicMock

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

    def test_get_exchange_rate_does_not_commit_outer_pending_transaction(self):
        d = date(2026, 11, 16)
        acc = Account(
            name="Test Bank",
            account_type="checking",
            tier="cash",
            institution="ASB",
            currency="SGD",
        )
        self.db.add(acc)
        self.db.commit()
        self.db.refresh(acc)

        pending_tx = Transaction(
            transaction_date=d,
            account_id=acc.id,
            amount=-45.0,
            currency="SGD",
            merchant="Coffee",
            category="Food",
            raw_hash="flush-safe-pending-tx",
        )
        self.db.add(pending_tx)
        self.db.flush()

        fetcher = StubFxFetcher({(d, "SGD", "NZD"): (1.3123, "stub")})
        resolved = get_exchange_rate(
            self.db,
            rate_date=d,
            from_currency="SGD",
            to_currency="NZD",
            fetcher=fetcher,
        )
        self.assertEqual(resolved, (1.3123, "stub"))

        # Both pending transaction and flushed rate must revert on outer rollback
        self.db.rollback()
        self.assertIsNone(
            self.db.scalar(select(Transaction).where(Transaction.raw_hash == "flush-safe-pending-tx"))
        )
        self.assertIsNone(
            self.db.scalar(
                select(ExchangeRate).where(
                    ExchangeRate.rate_date == d,
                    ExchangeRate.from_currency == "SGD",
                    ExchangeRate.to_currency == "NZD",
                )
            )
        )

    def test_cache_exchange_rate_savepoint_recovers_integrity_error_without_rolling_back_outer_tx(self):
        d = date(2026, 11, 17)
        acc = Account(
            name="Test Bank",
            account_type="checking",
            tier="cash",
            institution="ASB",
            currency="SGD",
        )
        self.db.add(acc)
        existing_rate = cache_exchange_rate(
            self.db,
            rate_date=d,
            from_currency="SGD",
            to_currency="NZD",
            rate=1.25,
            source="seed",
            commit=True,
        )
        self.db.refresh(acc)

        pending_tx = Transaction(
            transaction_date=d,
            account_id=acc.id,
            amount=-90.0,
            currency="SGD",
            merchant="Lunch",
            category="Food",
            raw_hash="savepoint-outer-tx",
        )
        self.db.add(pending_tx)
        self.db.flush()

        real_find = _find_cached_rate
        calls = {"count": 0}

        def simulated_race(db, *, rate_date, src, dst):
            calls["count"] += 1
            if calls["count"] == 1:
                return None
            return real_find(db, rate_date=rate_date, src=src, dst=dst)

        with patch("app.services.fx_service._find_cached_rate", side_effect=simulated_race):
            updated = cache_exchange_rate(
                self.db,
                rate_date=d,
                from_currency="SGD",
                to_currency="NZD",
                rate=1.34,
                source="wise",
                commit=False,
            )

        self.assertEqual(updated.id, existing_rate.id)
        self.assertAlmostEqual(float(updated.rate), 1.34, places=4)
        self.assertEqual(updated.source, "wise")
        # Outer uncommitted Transaction must still be alive in this session
        uncommitted_tx = self.db.scalar(
            select(Transaction).where(Transaction.raw_hash == "savepoint-outer-tx")
        )
        self.assertIsNotNone(uncommitted_tx)

        self.db.commit()
        self.db.refresh(uncommitted_tx)
        self.assertIsNotNone(uncommitted_tx.id)

    def test_http_fx_rate_fetcher_reuses_client_and_respects_ownership(self):
        seen_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            if "frankfurter" in request.url.host:
                return httpx.Response(200, json={"rates": {"NZD": 1.305}})
            if "coinbase" in request.url.host:
                return httpx.Response(200, json={"data": {"amount": "165000.0"}})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        external_client = httpx.Client(transport=transport)
        try:
            ext_fetcher = HttpFxRateFetcher(client=external_client)
            with ext_fetcher:
                r1 = ext_fetcher.fetch_rate(
                    rate_date=date(2026, 11, 14),
                    from_currency="SGD",
                    to_currency="NZD",
                )
                r2 = ext_fetcher.fetch_rate(
                    rate_date=date(2024, 1, 10),
                    from_currency="BTC",
                    to_currency="NZD",
                )
                self.assertIs(ext_fetcher._get_client(), external_client)
            self.assertEqual(r1, (1.305, "frankfurter"))
            self.assertEqual(r2, (165000.0, "coinbase"))
            # Externally supplied client must remain open after fetcher context exit
            self.assertFalse(external_client.is_closed)
            self.assertEqual(len(seen_urls), 2)
        finally:
            external_client.close()

        # Owned client is created lazily once, reused across calls, and closed on __exit__
        with HttpFxRateFetcher() as owned_fetcher:
            c1 = owned_fetcher._get_client()
            c2 = owned_fetcher._get_client()
            self.assertIs(c1, c2)
            self.assertFalse(c1.is_closed)
        self.assertTrue(c1.is_closed)
        self.assertIsNone(owned_fetcher._client)

    def test_resolve_reporting_rate_handles_nzd_rounding_and_missing_rates(self):
        d = date(2026, 11, 18)
        fetcher = StubFxFetcher(
            {
                (d, "SGD", "NZD"): (1.28123456789, "stub"),
                (d, "BAD", "NZD"): (0.0, "stub"),
                (d, "NEG", "NZD"): (-2.0, "stub"),
            }
        )
        self.assertIsNone(resolve_reporting_rate(self.db, "NZD", d, fetcher=fetcher))
        self.assertEqual(
            resolve_reporting_rate(self.db, " nzd ", d, fetcher=fetcher, nzd_value=1.0),
            1.0,
        )
        self.assertEqual(
            resolve_reporting_rate(self.db, "sgd", d, fetcher=fetcher),
            1.28123457,
        )
        self.assertIsNone(resolve_reporting_rate(self.db, "BAD", d, fetcher=fetcher))
        self.assertIsNone(resolve_reporting_rate(self.db, "NEG", d, fetcher=fetcher))
        self.assertIsNone(resolve_reporting_rate(self.db, "JPY", d, fetcher=fetcher))


if __name__ == "__main__":
    unittest.main()
