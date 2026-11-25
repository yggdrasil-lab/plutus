"""Unit and FastAPI integration tests for Plutus LLM Freedom Runway synthesizer."""

from __future__ import annotations

from datetime import date, timedelta
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
from app.models import Account, AssetSnapshot, BudgetRule, Loan
from app.services.ai_gateway import (
    AiInvalidResponseError,
    AiUpstreamError,
    InMemoryAiGateway,
    get_ai_gateway,
)
from app.services.llm_synthesizer import (
    synthesize_runway_deterministic,
    synthesize_runway_scenario_llm,
)
from app.services.runway import (
    AccountBalanceSnapshot,
    BalanceTierSnapshot,
    RunwaySnapshot,
    TierLabels,
    build_scenario_simulation,
)


def _make_sample_snapshot(*, annual_return: float = 0.05) -> RunwaySnapshot:
    labels = TierLabels(cash="Alex Checking 1001", growth="Index Fund", locked="KiwiSaver")
    balances = BalanceTierSnapshot(
        cash_balance=35000.0,
        growth_balance=150000.0,
        locked_balance=65000.0,
        total_liquid_balance=185000.0,
        tier_labels=labels,
        accounts=(
            AccountBalanceSnapshot(
                account_id=1001,
                account_name="Alex Checking 1001",
                account_type="checking",
                tier="cash",
                currency="NZD",
                snapshot_date=date(2026, 11, 20),
                balance=35000.0,
                balance_nzd=35000.0,
            ),
        ),
    )
    return RunwaySnapshot(
        as_of_date=date(2026, 11, 20),
        annual_return=annual_return,
        balances=balances,
        cash_balance=35000.0,
        growth_balance=150000.0,
        locked_balance=65000.0,
        total_liquid_balance=185000.0,
        total_liabilities=20000.0,
        net_worth=230000.0,
        emergency_weeks=29.17,
        growth_weeks=181.25,
        locked_weeks=54.2,
        primary_runway_weeks=181.25,
        primary_runway_years=3.49,
        is_perpetual=False,
        perpetual_threshold_weekly_income=173.63,
        observed_weekly_burn=1150.0,
        effective_weekly_burn=1200.0,
        variable_weekly_burn=500.0,
        recurring_weekly_burn=700.0,
        this_week_variable_spend=180.0,
        this_week_spend=880.0,
        weekly_budget_target=1200.0,
        time_banked_this_week=0.27,
        tier_labels=labels,
        loan_free_date=date(2028, 11, 20),
        loan_weekly_relief=250.0,
    )


class TestPlutusLlmSynthesizer(unittest.TestCase):
    def setUp(self) -> None:
        self.baseline = _make_sample_snapshot(annual_return=0.07)

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
        simulation = build_scenario_simulation(
            self.baseline,
            weekly_burn_adjustment=-150.0,
            pay_off_loans=True,
        )

        res = synthesize_runway_scenario_llm(
            simulation,
            custom_context="Planning to take 3 months sabbatical for badminton training",
            gateway=gateway,
        )
        self.assertTrue(res.is_llm_generated)
        self.assertIn("Runway extends", res.headline)
        self.assertEqual(len(res.recommendations), 2)
        self.assertIn("Trinity Study", res.research_citation)
        self.assertEqual(res.simulated_runway_weeks, simulation.simulated_runway_weeks)
        self.assertEqual(res.simulated_runway_years, simulation.simulated_runway_years)
        self.assertEqual(
            res.delta_weeks_compared_to_baseline,
            simulation.delta_weeks_compared_to_baseline,
        )
        self.assertEqual(len(gateway.text_requests), 1)
        sent_req = gateway.text_requests[0]
        self.assertEqual(sent_req.temperature, 0.2)
        self.assertEqual(sent_req.timeout, 12.0)
        self.assertIn("sabbatical", sent_req.user_prompt)

        prompt_payload = json.loads(sent_req.user_prompt.split(":\n", 1)[1])
        telemetry = prompt_payload["financial_telemetry"]
        adjustments = prompt_payload["scenario_adjustments"]
        self.assertEqual(telemetry["total_liabilities_nzd"], 20000.0)
        self.assertEqual(telemetry["net_worth_nzd"], 230000.0)
        self.assertEqual(telemetry["observed_weekly_burn_nzd"], 1150.0)
        self.assertEqual(telemetry["locked_weeks"], 54.2)
        self.assertEqual(telemetry["baseline_real_annual_return"], 0.07)
        self.assertTrue(adjustments["pay_off_loans"])
        self.assertEqual(adjustments["remaining_liabilities_nzd"], 0.0)
        self.assertEqual(adjustments["simulated_net_worth_nzd"], 230000.0)
        self.assertEqual(adjustments["expected_real_annual_return"], 0.07)

    def test_synthesize_runway_scenario_fallback_on_gateway_error(self) -> None:
        simulation = build_scenario_simulation(self.baseline, weekly_burn_adjustment=-100.0)
        for err in (
            AiUpstreamError("Connection refused", status_code=503),
            AiInvalidResponseError("Invalid JSON"),
        ):
            gateway = InMemoryAiGateway(text_responses=[err])
            res = synthesize_runway_scenario_llm(simulation, gateway=gateway)
            self.assertFalse(res.is_llm_generated)
            self.assertIsNotNone(res.headline)
            self.assertGreaterEqual(len(res.recommendations), 1)
            self.assertIn("Kitces", res.research_citation)
            self.assertEqual(res.simulated_runway_weeks, simulation.simulated_runway_weeks)

    def test_synthesize_runway_scenario_fallback_on_validation_error(self) -> None:
        simulation = build_scenario_simulation(self.baseline, weekly_burn_adjustment=-100.0)
        gateway = InMemoryAiGateway(
            text_responses=[
                {
                    "headline": 123,
                    "recommendations": "not-a-list",
                    "rationale": None,
                }
            ]
        )
        res = synthesize_runway_scenario_llm(simulation, gateway=gateway)
        self.assertFalse(res.is_llm_generated)
        self.assertIsNotNone(res.headline)
        self.assertGreaterEqual(len(res.recommendations), 1)
        self.assertIn("Kitces", res.research_citation)
        self.assertEqual(len(gateway.text_requests), 1)

    def test_synthesize_runway_deterministic_direct_call(self) -> None:
        simulation = build_scenario_simulation(self.baseline, lump_sum_adjustment=5000.0)
        res = synthesize_runway_deterministic(simulation, custom_context="Emergency buffer check")
        self.assertFalse(res.is_llm_generated)
        self.assertEqual(res.simulated_runway_weeks, simulation.simulated_runway_weeks)
        self.assertIn("Emergency buffer check", res.rationale)


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

    def test_post_runway_synthesize_matches_post_runway_simulate_with_active_loans(self) -> None:
        today = date.today()
        self.db.add(
            Loan(
                name="Vehicle Finance",
                lender="Civic Finance",
                loan_type="borrowed",
                principal_amount=18000.0,
                annual_interest_rate=0.06,
                term_months=24,
                monthly_payment=797.77,
                start_date=today - timedelta(days=30),
                is_active=True,
            )
        )
        self.db.commit()

        for pay_off_loans in (False, True):
            sim_res = self.client.post(
                "/runway/simulate",
                json={
                    "weekly_burn_adjustment": -50.0,
                    "lump_sum_adjustment": 4000.0,
                    "pay_off_loans": pay_off_loans,
                },
            )
            self.assertEqual(sim_res.status_code, 200)
            sim_body = sim_res.json()

            gateway = InMemoryAiGateway(
                text_responses=[
                    {
                        "headline": "Loan strategy evaluated",
                        "recommendations": ["Protect cash buffer."],
                        "rationale": "Computed from unified ScenarioSimulation.",
                        "research_citation": "Trinity Study",
                        "impact_summary": "Consistent across simulate and synthesize.",
                    }
                ]
            )
            app.dependency_overrides[get_ai_gateway] = (lambda gw: (lambda: gw))(gateway)

            syn_res = self.client.post(
                "/runway/synthesize",
                json={
                    "weekly_burn_adjustment": -50.0,
                    "lump_sum_adjustment": 4000.0,
                    "pay_off_loans": pay_off_loans,
                    "custom_context": "Evaluate loan payoff",
                },
            )
            self.assertEqual(syn_res.status_code, 200)
            syn_body = syn_res.json()
            self.assertTrue(syn_body["is_llm_generated"])
            self.assertEqual(syn_body["simulated_runway_weeks"], sim_body["simulated_runway_weeks"])
            self.assertEqual(syn_body["simulated_runway_years"], sim_body["simulated_runway_years"])
            self.assertEqual(syn_body["is_perpetual"], sim_body["is_perpetual"])
            self.assertEqual(
                syn_body["delta_weeks_compared_to_baseline"],
                sim_body["delta_weeks_compared_to_baseline"],
            )

            sent_payload = json.loads(gateway.text_requests[0].user_prompt.split(":\n", 1)[1])
            self.assertEqual(sent_payload["financial_telemetry"]["total_liabilities_nzd"], 18000.0)
            self.assertEqual(sent_payload["scenario_adjustments"]["pay_off_loans"], pay_off_loans)
            self.assertEqual(
                sent_payload["scenario_adjustments"]["remaining_liabilities_nzd"],
                sim_body["total_liabilities"],
            )
            self.assertEqual(
                sent_payload["scenario_adjustments"]["simulated_net_worth_nzd"],
                sim_body["simulated_net_worth"],
            )


if __name__ == "__main__":
    unittest.main()
