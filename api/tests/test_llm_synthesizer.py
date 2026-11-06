"""Unit tests for Plutus LLM Freedom Runway synthesizer."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from app.services.llm_synthesizer import (
    synthesize_runway_deterministic,
    synthesize_runway_scenario_llm,
)


class TestPlutusLlmSynthesizer(unittest.TestCase):
    def setUp(self):
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

    @patch("urllib.request.urlopen")
    def test_synthesize_runway_scenario_llm_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "headline": "Runway extends +16 weeks with $150/w burn titration",
                        "recommendations": [
                            "Titrate discretionary food and leisure burn by $150/week.",
                            "Keep minimum 26 weeks liquid cash buffer intact.",
                        ],
                        "rationale": "High liquid net worth supports extended autonomy with modest spending control.",
                        "research_citation": "Trinity Study (Safe Withdrawal) & Kitces (Dynamic Burn Titration)",
                        "impact_summary": "Liquid runway extends to 194 weeks.",
                    })
                }
            }]
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = synthesize_runway_scenario_llm(
            runway_data=self.sample_telemetry,
            burn_adjustment=-150.0,
            custom_context="Planning to take 3 months sabbatical for badminton training",
        )
        self.assertTrue(res.is_llm_generated)
        self.assertIn("Runway extends", res.headline)
        self.assertEqual(len(res.recommendations), 2)
        self.assertIn("Trinity Study", res.research_citation)

    @patch("urllib.request.urlopen")
    def test_synthesize_runway_scenario_fallback_on_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        res = synthesize_runway_scenario_llm(
            runway_data=self.sample_telemetry,
            burn_adjustment=-100.0,
        )
        self.assertFalse(res.is_llm_generated)
        self.assertIsNotNone(res.headline)
        self.assertGreaterEqual(len(res.recommendations), 1)
        self.assertIn("Kitces", res.research_citation)


if __name__ == "__main__":
    unittest.main()
