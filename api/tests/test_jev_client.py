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
    MAX_TYPESAFE_CHOICE_OPTIONS,
    TRANSACTION_GATES_SCHEMA,
    VALID_TYPESAFE_QUESTION_TYPES,
    build_transaction_gates_schema,
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

    def test_schema_uses_only_valid_typesafe_gate_types(self):
        allowed_gates = {"choice", "noul", "score"}
        self.assertEqual(VALID_TYPESAFE_QUESTION_TYPES, frozenset({"choice", "noul", "score", "box"}))
        self.assertEqual(MAX_TYPESAFE_CHOICE_OPTIONS, 255)
        self.assertNotIn("tags", TRANSACTION_GATES_SCHEMA["questions"])
        self.assertEqual(TRANSACTION_GATES_SCHEMA["questions"]["is_subscription"]["type"], "noul")

        for gate_name, gate in TRANSACTION_GATES_SCHEMA["questions"].items():
            self.assertIn(gate["type"], allowed_gates, f"Invalid base gate type on {gate_name}")
            self.assertNotEqual(gate["type"], "list")

        built = build_transaction_gates_schema(
            existing_tags=["gym", "coffee"],
            extra_categories=["Pets"],
        )
        for gate_name, gate in built["questions"].items():
            self.assertIn(gate["type"], allowed_gates, f"Invalid dynamic gate type on {gate_name}")
            self.assertNotEqual(gate["type"], "list")

    def test_build_schema_merges_extra_categories_and_caps_at_255(self):
        schema = build_transaction_gates_schema(
            extra_categories=["Pets", "  Hobby Gear  ", "Fitness", ""],
        )
        criteria = schema["questions"]["category"]["criteria"]
        self.assertEqual(criteria["Pets"], "Custom user category: Pets.")
        self.assertEqual(criteria["Hobby Gear"], "Custom user category: Hobby Gear.")
        # Existing base category description is preserved
        self.assertNotIn("Custom user category", criteria["Fitness"])
        # Base global schema must not be mutated
        self.assertNotIn("Pets", TRANSACTION_GATES_SCHEMA["questions"]["category"]["criteria"])

        many_extra = [f"CustomCat{i}" for i in range(300)]
        capped_schema = build_transaction_gates_schema(extra_categories=many_extra)
        self.assertEqual(
            len(capped_schema["questions"]["category"]["criteria"]),
            MAX_TYPESAFE_CHOICE_OPTIONS,
        )

    def test_build_schema_primary_tag_vocab_cleaning_exclusion_and_cap(self):
        # Empty or reserved-only existing_tags omits primary_tag gate
        empty_schema = build_transaction_gates_schema(existing_tags=[])
        self.assertNotIn("primary_tag", empty_schema["questions"])

        reserved_only = build_transaction_gates_schema(
            existing_tags=["General", "subscription", "#none", "  "],
        )
        self.assertNotIn("primary_tag", reserved_only["questions"])

        # Cleaning, deduplication, exclusion, and 250-tag cap (+ 'none' = <= 251)
        long_tag = "x" * 50
        raw_tags = [
            "general",
            "subscription",
            "none",
            "#Gym",
            "gym",
            "Coffee Beans",
            long_tag,
        ] + [f"t-{i}" for i in range(260)]
        schema = build_transaction_gates_schema(existing_tags=raw_tags)
        primary_gate = schema["questions"]["primary_tag"]
        self.assertEqual(primary_gate["type"], "choice")
        criteria = primary_gate["criteria"]
        self.assertLessEqual(len(criteria), 251)
        self.assertEqual(len(criteria), 251)
        self.assertIn("gym", criteria)
        self.assertIn("coffee-beans", criteria)
        self.assertIn("x" * 40, criteria)
        self.assertNotIn(long_tag, criteria)
        self.assertNotIn("general", criteria)
        self.assertNotIn("subscription", criteria)
        self.assertEqual(criteria["none"], "No existing tag applies")

    @patch("app.services.jev_client.call_jev")
    def test_classify_with_primary_tag_and_subscription_gates(self, mock_call_jev):
        mock_call_jev.return_value = (
            json.dumps(
                {
                    "category": "Fitness",
                    "is_anomaly": "false",
                    "primary_tag": "gym",
                    "is_subscription": "true",
                }
            ),
            0.01,
        )
        cat, is_anomaly, tags = classify_transaction_jev_with_tags(
            "CityFitness",
            -19.99,
            existing_tags=["gym", "coffee"],
            extra_categories=["Pets"],
        )
        self.assertEqual((cat, is_anomaly, tags), ("Fitness", False, ["gym", "subscription"]))

        system_prompt = mock_call_jev.call_args[0][0]
        schema_json = system_prompt.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
        sent_schema = json.loads(schema_json)
        self.assertIn("Pets", sent_schema["questions"]["category"]["criteria"])
        self.assertEqual(sent_schema["questions"]["is_subscription"]["type"], "noul")
        self.assertEqual(sent_schema["questions"]["primary_tag"]["type"], "choice")
        self.assertIn("gym", sent_schema["questions"]["primary_tag"]["criteria"])
        self.assertIn("none", sent_schema["questions"]["primary_tag"]["criteria"])
        for gate in sent_schema["questions"].values():
            self.assertIn(gate["type"], {"choice", "noul", "score"})

    @patch("app.services.jev_client.call_jev")
    def test_classify_with_primary_tag_none_and_category_strip(self, mock_call_jev):
        mock_call_jev.return_value = (
            json.dumps(
                {
                    "category": "Dining",
                    "is_anomaly": "false",
                    "primary_tag": "none",
                    "is_subscription": "false",
                }
            ),
            0.01,
        )
        self.assertEqual(
            classify_transaction_jev_with_tags("Cafe Mojo", -6.50, existing_tags=["coffee"]),
            ("Dining", False, []),
        )

        mock_call_jev.return_value = (
            json.dumps(
                {
                    "category": "Fitness",
                    "is_anomaly": "false",
                    "primary_tag": "fitness",
                    "is_subscription": "false",
                }
            ),
            0.01,
        )
        self.assertEqual(
            classify_transaction_jev_with_tags("CityFitness", -25.00, existing_tags=["fitness"]),
            ("Fitness", False, []),
        )

    @patch("app.services.jev_client.call_jev")
    def test_classify_extra_category_resolved_from_schema(self, mock_call_jev):
        mock_call_jev.return_value = (
            json.dumps(
                {
                    "category": "Hobbies",
                    "is_anomaly": "false",
                    "primary_tag": "none",
                    "is_subscription": "false",
                }
            ),
            0.01,
        )
        cat, is_anomaly, tags = classify_transaction_jev_with_tags(
            "Model Shop",
            -42.00,
            extra_categories=["Hobbies"],
        )
        self.assertEqual((cat, is_anomaly, tags), ("Hobbies", False, []))

    @patch("app.services.jev_client.call_jev")
    def test_classify_backward_compatible_tags_list_filters_none_and_caps_at_3(self, mock_call_jev):
        mock_call_jev.return_value = (
            json.dumps(
                {
                    "category": "Shopping",
                    "is_anomaly": "false",
                    "tags": ["none", "general", "shopping", "tech", "home", "gift", "extra"],
                }
            ),
            0.01,
        )
        cat, is_anomaly, tags = classify_transaction_jev_with_tags("Kmart", -50.00)
        self.assertEqual((cat, is_anomaly, tags), ("Shopping", False, ["tech", "home", "gift"]))


if __name__ == "__main__":
    unittest.main()
