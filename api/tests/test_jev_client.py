"""Unit tests for Plutus TypeSafe Jev client and transaction categorization."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base
from app.models import Category
from app.services.ai_gateway import (
    AiUpstreamError,
    InMemoryAiGateway,
    MuninnHttpAdapter,
)
from app.services.category_taxonomy import DEFAULT_CATEGORY_NAMES, ensure_default_categories
from app.services.jev_client import (
    MAX_TYPESAFE_CHOICE_OPTIONS,
    TRANSACTION_GATES_SCHEMA,
    VALID_JEV_CATEGORIES,
    VALID_TYPESAFE_QUESTION_TYPES,
    _validate_typesafe_schema,
    build_transaction_gates_schema,
    call_jev,
    classify_transaction_jev,
    classify_transaction_jev_with_tags,
    get_extra_jev_categories,
    is_jev_circuit_open,
    reset_jev_circuit,
)


class FakeClock:
    def __init__(self, initial: float = 0.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now


class TestPlutusJevClient(unittest.TestCase):
    def test_call_jev_success_via_in_memory_gateway(self) -> None:
        gateway = InMemoryAiGateway(
            jev_responses=[json.dumps({"category": "Groceries", "is_anomaly": "false"})]
        )
        verdict, latency = call_jev(
            "Categorize expense",
            "Merchant: Pak n Save, Amount: $120.00",
            gateway=gateway,
        )
        self.assertIsNotNone(verdict)
        self.assertGreaterEqual(latency, 0.0)
        self.assertEqual(len(gateway.jev_requests), 1)
        self.assertEqual(gateway.jev_requests[0].system_prompt, "Categorize expense")

    def test_call_jev_upstream_error_returns_none(self) -> None:
        gateway = InMemoryAiGateway(
            jev_responses=[AiUpstreamError("Connection refused", status_code=503)]
        )
        verdict, latency = call_jev(
            "Categorize expense",
            "Merchant: Pak n Save, Amount: $120.00",
            gateway=gateway,
        )
        self.assertIsNone(verdict)
        self.assertEqual(latency, 0.0)

    def test_classify_transaction_jev_routine_and_anomaly(self) -> None:
        gateway = InMemoryAiGateway(
            jev_responses=[
                json.dumps({"category": "Transport", "is_anomaly": "false"}),
                json.dumps({"category": "Food", "is_anomaly": "true"}),
                AiUpstreamError("Timed out"),
            ]
        )
        cat1, anomaly1 = classify_transaction_jev("BP Connect Takapuna", 85.00, gateway=gateway)
        self.assertEqual((cat1, anomaly1), ("Transport", False))

        cat2, anomaly2 = classify_transaction_jev("Luxury Steakhouse", 1250.00, gateway=gateway)
        self.assertEqual((cat2, anomaly2), ("Food", True))

        cat3, anomaly3 = classify_transaction_jev("Unknown Store", 45.00, gateway=gateway)
        self.assertEqual((cat3, anomaly3), ("General", False))

    def test_circuit_breaker_opens_after_consecutive_failures_on_injected_adapter(self) -> None:
        calls = 0

        def fail_handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503, text="unavailable")

        adapter = MuninnHttpAdapter(
            base_url="http://muninn:4000/v1",
            api_key="test-key",
            default_jev_model="jev-decision",
            transport=httpx.MockTransport(fail_handler),
            clock=FakeClock(10.0),
        )
        for _ in range(3):
            verdict, _ = call_jev(
                "Categorize expense",
                "Merchant: Test, Amount: $10.00",
                gateway=adapter,
            )
            self.assertIsNone(verdict)
        self.assertTrue(is_jev_circuit_open(gateway=adapter))
        self.assertEqual(calls, 3)

        # Subsequent call short-circuits without invoking transport
        verdict, latency = call_jev(
            "Categorize expense",
            "Merchant: Test2, Amount: $20.00",
            gateway=adapter,
        )
        self.assertIsNone(verdict)
        self.assertEqual(latency, 0.0)
        self.assertEqual(calls, 3)

        reset_jev_circuit(gateway=adapter)
        self.assertFalse(is_jev_circuit_open(gateway=adapter))

    def test_classify_with_tags_vocab_cap_80_extra_cats_and_tag_filtering(self) -> None:
        gateway = InMemoryAiGateway(
            jev_responses=[
                json.dumps(
                    {
                        "category": "Pets",
                        "is_anomaly": "false",
                        "tags": ["General", "#Pets", "vet-care", "dog-food"],
                    }
                )
            ]
        )
        vocab = ["general"] + [f"tag-{i}" for i in range(90)]
        cat, is_anomaly, tags = classify_transaction_jev_with_tags(
            "Animates",
            -45.0,
            notes="Checkup",
            existing_tags=vocab,
            extra_categories=["Pets"],
            gateway=gateway,
        )
        self.assertEqual(cat, "Pets")
        self.assertFalse(is_anomaly)
        self.assertEqual(tags, ["vet-care", "dog-food"])

        user_prompt = gateway.jev_requests[0].user_content
        self.assertIn("tag-79", user_prompt)
        self.assertNotIn("tag-80", user_prompt)
        self.assertNotIn(", general,", user_prompt)
        self.assertIn("Additional custom categories: Pets", user_prompt)

    def test_classify_with_tags_filters_multiword_category_kebab_tag(self) -> None:
        gateway = InMemoryAiGateway(
            jev_responses=[
                json.dumps(
                    {
                        "category": "Personal Care",
                        "is_anomaly": False,
                        "tags": ["Personal Care", "general", "skincare"],
                    }
                )
            ]
        )
        cat, _, tags = classify_transaction_jev_with_tags("Mecca", -60.0, gateway=gateway)
        self.assertEqual(cat, "Personal Care")
        self.assertEqual(tags, ["skincare"])

    def test_base_categories_derive_from_category_taxonomy(self) -> None:
        self.assertEqual(len(DEFAULT_CATEGORY_NAMES), 33)
        self.assertEqual(VALID_JEV_CATEGORIES, frozenset(DEFAULT_CATEGORY_NAMES))
        self.assertEqual(
            tuple(TRANSACTION_GATES_SCHEMA["questions"]["category"]["criteria"].keys()),
            DEFAULT_CATEGORY_NAMES,
        )

    def test_validate_typesafe_schema_rejects_invalid_types_or_overflow(self) -> None:
        with self.assertRaises(ValueError):
            _validate_typesafe_schema({"questions": {"bad": {"type": "list"}}})

        too_many = {f"C{i}": f"desc {i}" for i in range(MAX_TYPESAFE_CHOICE_OPTIONS + 1)}
        with self.assertRaises(ValueError):
            _validate_typesafe_schema({"questions": {"category": {"type": "choice", "criteria": too_many}}})

    def test_get_extra_jev_categories_returns_only_active_custom_categories(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)
        with SessionLocal() as db:
            self.assertEqual(get_extra_jev_categories(db), [])

            ensure_default_categories(db)
            self.assertEqual(get_extra_jev_categories(db), [])

            db.add(Category(name="Pets", is_active=True, is_system=False, sort_order=150))
            db.add(Category(name="Archived Cat", is_active=False, is_system=False, sort_order=151))
            db.commit()

            self.assertEqual(get_extra_jev_categories(db), ["Pets"])

    def test_schema_uses_only_valid_typesafe_gate_types(self) -> None:
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

    def test_build_schema_merges_extra_categories_and_caps_at_255(self) -> None:
        schema = build_transaction_gates_schema(
            extra_categories=["Pets", "  Hobby Gear  ", "Fitness", ""],
        )
        criteria = schema["questions"]["category"]["criteria"]
        self.assertEqual(criteria["Pets"], "Custom user category: Pets.")
        self.assertEqual(criteria["Hobby Gear"], "Custom user category: Hobby Gear.")
        self.assertNotIn("Custom user category", criteria["Fitness"])
        self.assertNotIn("Pets", TRANSACTION_GATES_SCHEMA["questions"]["category"]["criteria"])

        many_extra = [f"CustomCat{i}" for i in range(300)]
        capped_schema = build_transaction_gates_schema(extra_categories=many_extra)
        self.assertEqual(
            len(capped_schema["questions"]["category"]["criteria"]),
            MAX_TYPESAFE_CHOICE_OPTIONS,
        )

    def test_build_schema_primary_tag_vocab_cleaning_exclusion_and_cap(self) -> None:
        empty_schema = build_transaction_gates_schema(existing_tags=[])
        self.assertNotIn("primary_tag", empty_schema["questions"])

        reserved_only = build_transaction_gates_schema(
            existing_tags=["General", "subscription", "#none", "  "],
        )
        self.assertNotIn("primary_tag", reserved_only["questions"])

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
        self.assertEqual(len(criteria), 251)
        self.assertIn("gym", criteria)
        self.assertIn("coffee-beans", criteria)
        self.assertIn("x" * 40, criteria)
        self.assertNotIn(long_tag, criteria)
        self.assertNotIn("general", criteria)
        self.assertNotIn("subscription", criteria)
        self.assertEqual(criteria["none"], "No existing tag applies")

    def test_classify_with_primary_tag_and_subscription_gates(self) -> None:
        gateway = InMemoryAiGateway(
            jev_responses=[
                json.dumps(
                    {
                        "category": "Fitness",
                        "is_anomaly": "false",
                        "primary_tag": "gym",
                        "is_subscription": "true",
                    }
                )
            ]
        )
        cat, is_anomaly, tags = classify_transaction_jev_with_tags(
            "CityFitness",
            -19.99,
            existing_tags=["gym", "coffee"],
            extra_categories=["Pets"],
            gateway=gateway,
        )
        self.assertEqual((cat, is_anomaly, tags), ("Fitness", False, ["gym", "subscription"]))

        system_prompt = gateway.jev_requests[0].system_prompt
        schema_json = system_prompt.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
        sent_schema = json.loads(schema_json)
        self.assertIn("Pets", sent_schema["questions"]["category"]["criteria"])
        self.assertEqual(sent_schema["questions"]["is_subscription"]["type"], "noul")
        self.assertEqual(sent_schema["questions"]["primary_tag"]["type"], "choice")
        self.assertIn("gym", sent_schema["questions"]["primary_tag"]["criteria"])
        self.assertIn("none", sent_schema["questions"]["primary_tag"]["criteria"])
        for gate in sent_schema["questions"].values():
            self.assertIn(gate["type"], {"choice", "noul", "score"})

    def test_classify_with_primary_tag_none_and_category_strip(self) -> None:
        gateway = InMemoryAiGateway(
            jev_responses=[
                json.dumps(
                    {
                        "category": "Dining",
                        "is_anomaly": "false",
                        "primary_tag": "none",
                        "is_subscription": "false",
                    }
                ),
                json.dumps(
                    {
                        "category": "Fitness",
                        "is_anomaly": "false",
                        "primary_tag": "fitness",
                        "is_subscription": "false",
                    }
                ),
            ]
        )
        self.assertEqual(
            classify_transaction_jev_with_tags(
                "Cafe Mojo",
                -6.50,
                existing_tags=["coffee"],
                gateway=gateway,
            ),
            ("Dining", False, []),
        )
        self.assertEqual(
            classify_transaction_jev_with_tags(
                "CityFitness",
                -25.00,
                existing_tags=["fitness"],
                gateway=gateway,
            ),
            ("Fitness", False, []),
        )

    def test_classify_extra_category_resolved_from_schema(self) -> None:
        gateway = InMemoryAiGateway(
            jev_responses=[
                json.dumps(
                    {
                        "category": "Hobbies",
                        "is_anomaly": "false",
                        "primary_tag": "none",
                        "is_subscription": "false",
                    }
                )
            ]
        )
        cat, is_anomaly, tags = classify_transaction_jev_with_tags(
            "Model Shop",
            -42.00,
            extra_categories=["Hobbies"],
            gateway=gateway,
        )
        self.assertEqual((cat, is_anomaly, tags), ("Hobbies", False, []))

    def test_classify_backward_compatible_tags_list_filters_none_and_caps_at_3(self) -> None:
        gateway = InMemoryAiGateway(
            jev_responses=[
                json.dumps(
                    {
                        "category": "Shopping",
                        "is_anomaly": "false",
                        "tags": ["none", "general", "shopping", "tech", "home", "gift", "extra"],
                    }
                )
            ]
        )
        cat, is_anomaly, tags = classify_transaction_jev_with_tags(
            "Kmart",
            -50.00,
            gateway=gateway,
        )
        self.assertEqual((cat, is_anomaly, tags), ("Shopping", False, ["tech", "home", "gift"]))


if __name__ == "__main__":
    unittest.main()
