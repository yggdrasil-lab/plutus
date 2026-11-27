"""Unit tests for the read-only GET /fx/rates endpoint."""

from datetime import date, datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base, get_db
from app.main import app
from app.models import ExchangeRate
from app.schemas import FxRateEntryRead, FxRatesRead


class TestFxRouter(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        Base.metadata.create_all(bind=self.engine)
        self.db = self.Session()

        def override_get_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_empty_exchange_rates_returns_identity_only(self):
        resp = self.client.get("/fx/rates")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(),
            {
                "to_currency": "NZD",
                "rates": {
                    "NZD": {
                        "rate": 1.0,
                        "rate_date": None,
                        "source": "identity",
                    }
                },
            },
        )

    def test_picks_latest_rate_date_and_created_at_per_currency(self):
        self.db.add_all(
            [
                ExchangeRate(
                    rate_date=date(2026, 11, 10),
                    from_currency="SGD",
                    to_currency="NZD",
                    rate=1.31,
                    source="frankfurter",
                    created_at=datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc),
                ),
                ExchangeRate(
                    rate_date=date(2026, 11, 18),
                    from_currency="SGD",
                    to_currency="NZD",
                    rate=1.35,
                    source="wise",
                    created_at=datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc),
                ),
                ExchangeRate(
                    rate_date=date(2026, 11, 15),
                    from_currency="PHP",
                    to_currency="NZD",
                    rate=0.0295,
                    source="frankfurter",
                    created_at=datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc),
                ),
                ExchangeRate(
                    rate_date=date(2026, 11, 18),
                    from_currency="JPY",
                    to_currency="NZD",
                    rate=0.0112,
                    source="frankfurter",
                    created_at=datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc),
                ),
                ExchangeRate(
                    rate_date=date(2026, 11, 19),
                    from_currency="USD",
                    to_currency="SGD",
                    rate=1.34,
                    source="wise",
                    created_at=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc),
                ),
            ]
        )
        self.db.commit()

        resp = self.client.get("/fx/rates")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["to_currency"], "NZD")
        self.assertEqual(set(body["rates"].keys()), {"NZD", "SGD", "PHP", "JPY"})
        self.assertAlmostEqual(body["rates"]["SGD"]["rate"], 1.35, places=4)
        self.assertEqual(body["rates"]["SGD"]["rate_date"], "2026-11-18")
        self.assertEqual(body["rates"]["SGD"]["source"], "wise")
        self.assertAlmostEqual(body["rates"]["PHP"]["rate"], 0.0295, places=4)
        self.assertEqual(body["rates"]["NZD"]["source"], "identity")

    def test_case_insensitive_currencies_filter_and_zero_writes(self):
        self.db.add_all(
            [
                ExchangeRate(
                    rate_date=date(2026, 11, 18),
                    from_currency="SGD",
                    to_currency="NZD",
                    rate=1.36,
                    source="wise",
                ),
                ExchangeRate(
                    rate_date=date(2026, 11, 18),
                    from_currency="PHP",
                    to_currency="NZD",
                    rate=0.0301,
                    source="frankfurter",
                ),
                ExchangeRate(
                    rate_date=date(2026, 11, 18),
                    from_currency="JPY",
                    to_currency="NZD",
                    rate=0.0112,
                    source="frankfurter",
                ),
            ]
        )
        self.db.commit()
        before_count = self.db.scalar(select(func.count()).select_from(ExchangeRate))

        with patch("app.services.fx_service.HttpFxRateFetcher.fetch_rate") as mock_fetch:
            resp = self.client.get("/fx/rates?currencies=sgd,php,eur")

        self.assertEqual(resp.status_code, 200)
        mock_fetch.assert_not_called()
        body = resp.json()
        self.assertEqual(set(body["rates"].keys()), {"NZD", "SGD", "PHP"})
        self.assertNotIn("JPY", body["rates"])
        self.assertNotIn("EUR", body["rates"])

        after_count = self.db.scalar(select(func.count()).select_from(ExchangeRate))
        self.assertEqual(before_count, after_count)

    def test_to_currency_normalization_and_validation(self):
        self.db.add(
            ExchangeRate(
                rate_date=date(2026, 11, 19),
                from_currency="USD",
                to_currency="SGD",
                rate=1.34,
                source="wise",
            )
        )
        self.db.commit()

        ok_resp = self.client.get("/fx/rates?to_currency=%20sgd%20")
        self.assertEqual(ok_resp.status_code, 200)
        ok_body = ok_resp.json()
        self.assertEqual(ok_body["to_currency"], "SGD")
        self.assertEqual(set(ok_body["rates"].keys()), {"SGD", "USD"})
        self.assertAlmostEqual(ok_body["rates"]["USD"]["rate"], 1.34, places=4)

        for bad_value in ("   ", "123", "NZ$", "ab"):
            bad_resp = self.client.get(f"/fx/rates?to_currency={bad_value}")
            self.assertEqual(bad_resp.status_code, 422, f"Expected 422 for {bad_value!r}")

    def test_schemas_are_frozen(self):
        entry = FxRateEntryRead(rate=1.35, rate_date=date(2026, 11, 18), source="wise")
        payload = FxRatesRead(to_currency="NZD", rates={"SGD": entry})
        with self.assertRaises(ValidationError):
            entry.rate = 2.0  # type: ignore[misc]
        with self.assertRaises(ValidationError):
            payload.to_currency = "SGD"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
