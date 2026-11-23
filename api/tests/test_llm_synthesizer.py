"""Unit and FastAPI integration tests for Plutus LLM Freedom Runway synthesizer."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sys
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base, get_db
from app.main import app
from app.models import Account, AssetSnapshot, BudgetRule
from app.services.ai_gateway import (
    AiInvalidResponseError,
    AiUpstreamError,
    InMemoryAiGateway,
    get_ai_gateway,
)
from app.services.llm_synthesizer import synthesize_runway_scenario_llm


class TestPlutusLlmSynthesizer(unittest.TestCase):
    def setUp(self) -> None:
        self.sample_telemetry = {
            "cash_balance": 35000.0,
            "growth_balance": 150000.0,
            "locked_balance": 65000.0,
            "total_liquid_balance": 185000.0,
            "weekly_burn_rate": 1200.0,
            "emergency_weeks": 29.2,
            "growth_weeks": 178.5,
            "is_perpetual": False,
        }

    def test_synthesize_runway_scenario_llm_success(self) -> None:
        gateway = InMemoryAiGateway(
            text_responses=[
                {
                    "headline": "Runway extends +16 weeks with $150/w burn titration",
                    "recommendations": [
                        "Titrate discretionary food and leisure burn by $150/week.",
                        "Keep minimum 26 weeks liquid cash buffer intact.",
                    ],
                    "rationale": "High liquid net worth supports extended autonomy with modest spending control.",
                    "research_citation": "Trinity Study (Safe Withdrawal) & Kitces (Dynamic Burn Titration)",
                    "impact_summary": "Liquid runway extends to 194 weeks.",
                }
            ]
        )

        res = synthesize_runway_scenario_llm(
            runway_data=self.sample_telemetry,
            burn_adjustment=-150.0,
            custom_context="Planning to take 3 months sabbatical for badminton training",
            gateway=gateway,
        )
        self.assertTrue(res.is_llm_generated)
        self.assertIn("Runway extends", res.headline)
        self.assertEqual(len(res.recommendations), 2)
        self.assertIn("Trinity Study", res.research_citation)
        self.assertEqual(len(gateway.text_requests), 1)
        sent_req = gateway.text_requests[0]
        self.assertEqual(sent_req.temperature, 0.2)
        self.assertEqual(sent_req.timeout, 12.0)
        self.assertIn("sabbatical", sent_req.user_prompt)

    def test_synthesize_runway_scenario_fallback_on_gateway_error(self) -> None:
        for err in (
            AiUpstreamError("Connection refused", status_code=503),
            AiInvalidResponseError("Invalid JSON"),
        ):
            gateway = InMemoryAiGateway(text_responses=[err])
            res = synthesize_runway_scenario_llm(
                runway_data=self.sample_telemetry,
                burn_adjustment=-100.0,
                gateway=gateway,
            )
            self.assertFalse(res.is_llm_generated)
            self.assertIsNotNone(res.headline)
            self.assertGreaterEqual(len(res.recommendations), 1)
            self.assertIn("Kitces", res.research_citation)

    def test_synthesize_runway_scenario_fallback_on_validation_error(self) -> None:
        gateway = InMemoryAiGateway(
            text_responses=[
                {
                    "headline": 123,
                    "recommendations": "not-a-list",
                    "rationale": None,
                }
            ]
        )
        res = synthesize_runway_scenario_llm(
            runway_data=self.sample_telemetry,
            burn_adjustment=-100.0,
            gateway=gateway,
        )
        self.assertFalse(res.is_llm_generated)
        self.assertIsNotNone(res.headline)
        self.assertGreaterEqual(len(res.recommendations), 1)
        self.assertIn("Kitces", res.research_citation)
        self.assertEqual(len(gateway.text_requests), 1)


class TestRunwaySynthesizeEndpoint(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = SessionLocal()

        def override_get_db():
            db = SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

        cash_acc = Account(
            name="ANZ Checking",
            account_type="checking",
            tier="cash",
            institution="ANZ",
            currency="NZD",
        )
        growth_acc = Account(
            name="Index Portfolio",
            account_type="brokerage",
            tier="growth",
            institution="Kernel",
            currency="NZD",
        )
        self.db.add_all([cash_acc, growth_acc])
        self.db.flush()
        today = date.today()
        self.db.add_all(
            [
                AssetSnapshot(
                    snapshot_date=today,
                    account_id=cash_acc.id,
                    balance=26000.0,
                    currency="NZD",
                    exchange_rate=1.0,
                    balance_nzd=26000.0,
                ),
                AssetSnapshot(
                    snapshot_date=today,
                    account_id=growth_acc.id,
                    balance=104000.0,
                    currency="NZD",
                    exchange_rate=1.0,
                    balance_nzd=104000.0,
                ),
                BudgetRule(category="Groceries", weekly_limit=250.0, monthly_limit=1083.33),
                BudgetRule(category="Rent", weekly_limit=600.0, monthly_limit=2600.0),
            ]
        )
        self.db.commit()

    def tearDown(self) -> None:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_ai_gateway, None)
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_post_runway_synthesize_uses_injected_gateway_success(self) -> None:
        gateway = InMemoryAiGateway(
            text_responses=[
                {
                    "headline": "Trim $100/w to gain +19 weeks autonomy",
                    "recommendations": [
                        "Maintain 26-week cash floor.",
                        "Invest surplus into broad equities.",
                    ],
                    "rationale": "Strong liquidity with modest weekly burn.",
                    "research_citation": "Trinity Study",
                    "impact_summary": "Runway extended cleanly.",
                }
            ]
        )
        app.dependency_overrides[get_ai_gateway] = lambda: gateway

        res = self.client.post(
            "/runway/synthesize",
            json={
                "weekly_burn_adjustment": -100.0,
                "lump_sum_adjustment": 10000.0,
                "custom_context": "Focus on liquid cash buffer",
            },
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["is_llm_generated"])
        self.assertEqual(body["headline"], "Trim $100/w to gain +19 weeks autonomy")
        self.assertEqual(len(body["recommendations"]), 2)
        self.assertEqual(len(gateway.text_requests), 1)
        sent_payload = json.loads(gateway.text_requests[0].user_prompt.split(":\n", 1)[1])
        self.assertEqual(sent_payload["financial_telemetry"]["total_liquid_nzd"], 130000.0)
        self.assertEqual(sent_payload["scenario_adjustments"]["burn_adjustment_per_week"], -100.0)
        self.assertEqual(sent_payload["scenario_adjustments"]["lump_sum_adjustment"], 10000.0)

    def test_post_runway_synthesize_falls_back_deterministically_on_gateway_error(self) -> None:
        gateway = InMemoryAiGateway(
            text_responses=[AiUpstreamError("Muninn unavailable", status_code=502)]
        )
        app.dependency_overrides[get_ai_gateway] = lambda: gateway

        res = self.client.post(
            "/runway/synthesize",
            json={"weekly_burn_adjustment": -50.0, "custom_context": "Build buffer"},
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertFalse(body["is_llm_generated"])
        self.assertIn("Trinity Study", body["research_citation"])
        self.assertIn("Build buffer", body["rationale"])
        self.assertEqual(len(gateway.text_requests), 1)


if __name__ == "__main__":
    unittest.main()
