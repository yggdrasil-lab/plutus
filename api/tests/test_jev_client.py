"""Unit tests for Plutus TypeSafe Jev client and transaction categorization."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Category
from app.services.category_taxonomy import ensure_default_categories
from app.services.jev_client import (
    CIRCUIT_FAILURE_THRESHOLD,
    call_jev,
    classify_transaction_jev,
    classify_transaction_jev_with_tags,
    get_extra_jev_categories,
    is_jev_circuit_open,
    reset_jev_circuit,
)
from app.services.period_service import resolve_period_dates


class TestPlutusJevClient(unittest.TestCase):
    def setUp(self):
        reset_jev_circuit()

    def tearDown(self):
        reset_jev_circuit()
    @patch("urllib.request.urlopen")
    def test_call_jev_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": json.dumps({"category": "Groceries", "is_anomaly": "false"})}}]
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        verdict, latency = call_jev("Categorize expense", "Merchant: Pak n Save, Amount: $120.00")
        self.assertIsNotNone(verdict)
        self.assertGreaterEqual(latency, 0.0)

    @patch("urllib.request.urlopen")
    def test_call_jev_urllib_error_fallback(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        verdict, latency = call_jev("Categorize expense", "Merchant: Pak n Save, Amount: $120.00")
        self.assertIsNone(verdict)

    @patch("app.services.jev_client.call_jev")
    def test_classify_transaction_jev_routine(self, mock_call_jev):
        mock_call_jev.return_value = (
            json.dumps({"category": "Transport", "is_anomaly": "false"}),
            0.02,
        )
        cat, is_anomaly = classify_transaction_jev("BP Connect Takapuna", 85.00)
        self.assertEqual(cat, "Transport")
        self.assertFalse(is_anomaly)

    @patch("app.services.jev_client.call_jev")
    def test_classify_transaction_jev_anomaly(self, mock_call_jev):
        mock_call_jev.return_value = (
            json.dumps({"category": "Food", "is_anomaly": "true"}),
            0.02,
        )
        cat, is_anomaly = classify_transaction_jev("Luxury Steakhouse", 1250.00)
        self.assertEqual(cat, "Food")
        self.assertTrue(is_anomaly)

    @patch("app.services.jev_client.call_jev")
    def test_classify_transaction_jev_fallback_on_empty(self, mock_call_jev):
        mock_call_jev.return_value = (None, 0.02)
        cat, is_anomaly = classify_transaction_jev("Unknown Store", 45.00)
        self.assertEqual(cat, "General")
        self.assertFalse(is_anomaly)

    @patch("urllib.request.urlopen")
    def test_circuit_breaker_opens_after_consecutive_failures(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Timed out")
        for _ in range(CIRCUIT_FAILURE_THRESHOLD):
            verdict, _ = call_jev("Categorize expense", "Merchant: Test, Amount: $10.00")
            self.assertIsNone(verdict)
        self.assertTrue(is_jev_circuit_open())
        self.assertEqual(mock_urlopen.call_count, CIRCUIT_FAILURE_THRESHOLD)

        # Subsequent call short-circuits without invoking urlopen
        verdict, latency = call_jev("Categorize expense", "Merchant: Test2, Amount: $20.00")
        self.assertIsNone(verdict)
        self.assertEqual(latency, 0.0)
        self.assertEqual(mock_urlopen.call_count, CIRCUIT_FAILURE_THRESHOLD)

    def test_resolve_period_dates_last_week_and_custom(self):
        ref = date(2026, 11, 17)  # Tuesday
        start, end = resolve_period_dates("last_week", today=ref)
        self.assertEqual(start, date(2026, 11, 9))
        self.assertEqual(end, date(2026, 11, 15))

        c_start, c_end = resolve_period_dates(None, date(2026, 10, 1), date(2026, 10, 15), today=ref)
        self.assertEqual(c_start, date(2026, 10, 1))
        self.assertEqual(c_end, date(2026, 10, 15))

    @patch("app.services.jev_client.call_jev")
    def test_classify_with_tags_vocab_cap_80_extra_cats_and_tag_filtering(self, mock_call_jev):
        mock_call_jev.return_value = (
            json.dumps(
                {
                    "category": "Pets",
                    "is_anomaly": "false",
                    "tags": ["General", "#Pets", "vet-care", "dog-food"],
                }
            ),
            0.01,
        )
        vocab = ["general"] + [f"tag-{i}" for i in range(90)]
        cat, is_anomaly, tags = classify_transaction_jev_with_tags(
            "Animates",
            -45.0,
            notes="Checkup",
            existing_tags=vocab,
            extra_categories=["Pets"],
        )
        self.assertEqual(cat, "Pets")
        self.assertFalse(is_anomaly)
        self.assertEqual(tags, ["vet-care", "dog-food"])

        user_prompt = mock_call_jev.call_args[0][1]
        self.assertIn("tag-79", user_prompt)
        self.assertNotIn("tag-80", user_prompt)
        self.assertNotIn(", general,", user_prompt)
        self.assertIn("Additional custom categories: Pets", user_prompt)

    @patch("app.services.jev_client.call_jev")
    def test_classify_with_tags_filters_multiword_category_kebab_tag(self, mock_call_jev):
        mock_call_jev.return_value = (
            json.dumps(
                {
                    "category": "Personal Care",
                    "is_anomaly": False,
                    "tags": ["Personal Care", "general", "skincare"],
                }
            ),
            0.01,
        )
        cat, _, tags = classify_transaction_jev_with_tags("Mecca", -60.0)
        self.assertEqual(cat, "Personal Care")
        self.assertEqual(tags, ["skincare"])

    def test_get_extra_jev_categories_returns_only_active_custom_categories(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)
        with SessionLocal() as db:
            # Unseeded DB returns empty list (defaults match VALID_JEV_CATEGORIES)
            self.assertEqual(get_extra_jev_categories(db), [])

            ensure_default_categories(db)
            self.assertEqual(get_extra_jev_categories(db), [])

            db.add(Category(name="Pets", is_active=True, is_system=False, sort_order=150))
            db.add(Category(name="Archived Cat", is_active=False, is_system=False, sort_order=151))
            db.commit()

            self.assertEqual(get_extra_jev_categories(db), ["Pets"])


if __name__ == "__main__":
    unittest.main()
