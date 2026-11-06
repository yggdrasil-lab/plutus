"""Unit tests for Plutus TypeSafe Jev client and transaction categorization."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from app.services.jev_client import (
    call_jev,
    classify_transaction_jev,
)


class TestPlutusJevClient(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
