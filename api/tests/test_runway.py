"""Unit tests for Plutus financial runway and freedom horizon mathematics."""

import math
import sys
import unittest
from pathlib import Path

# Add api directory to sys.path so app can be imported
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.runway import (
    calculate_compounding_runway,
    calculate_emergency_runway,
    calculate_time_banked,
    calculate_weekly_growth_rate,
)


class TestRunwayCalculations(unittest.TestCase):
    """Test runway math formulas across various boundary conditions."""

    def test_emergency_runway_zero_burn(self):
        """Zero or negative burn rate should safely return 0.0 weeks."""
        self.assertEqual(calculate_emergency_runway(10000.0, 0.0), 0.0)
        self.assertEqual(calculate_emergency_runway(10000.0, -100.0), 0.0)

    def test_emergency_runway_standard(self):
        """$10,000 cash at $1,000/week burn should yield exactly 10.0 weeks."""
        self.assertEqual(calculate_emergency_runway(10000.0, 1000.0), 10.0)
        self.assertEqual(calculate_emergency_runway(25000.0, 500.0), 50.0)

    def test_weekly_growth_rate(self):
        """Annual return of ~5.2% real return compounded weekly."""
        weekly_g = calculate_weekly_growth_rate(0.052)
        # (1 + 0.052)^(1/52) - 1 approx 0.000975
        self.assertAlmostEqual(weekly_g, 0.000975, places=5)

    def test_compounding_runway_zero_growth(self):
        """When growth rate is 0.0, compounding runway matches linear depletion."""
        weeks, is_perpetual = calculate_compounding_runway(50000.0, 1000.0, 0.0)
        self.assertFalse(is_perpetual)
        self.assertEqual(weeks, 50.0)

    def test_compounding_runway_positive_growth(self):
        """Asset growth extends runway significantly beyond linear depletion."""
        # $50,000 at $1,000/week with 6% real annual return
        weeks, is_perpetual = calculate_compounding_runway(50000.0, 1000.0, 0.06)
        self.assertFalse(is_perpetual)
        self.assertIsNotNone(weeks)
        # Without growth it would be 50.0 weeks; with 6% growth it should be ~51.4 weeks
        self.assertGreater(weeks, 50.0)

    def test_perpetual_freedom_threshold(self):
        """When weekly yield >= weekly burn, runway becomes infinite (perpetual)."""
        # $1,200,000 at 5% real return generates ~$1,120/week yield > $1,000/week burn
        weeks, is_perpetual = calculate_compounding_runway(1200000.0, 1000.0, 0.05)
        self.assertTrue(is_perpetual)
        self.assertIsNone(weeks)

    def test_time_banked(self):
        """Test calculation of weeks banked relative to budget."""
        # Spent $750 against $1,000 budget = +0.25 weeks banked (1.75 days)
        self.assertEqual(calculate_time_banked(1000.0, 750.0), 0.25)
        # Spent $1,200 against $1,000 budget = -0.20 weeks deficit
        self.assertEqual(calculate_time_banked(1000.0, 1200.0), -0.20)
        # Zero budget edge case
        self.assertEqual(calculate_time_banked(0.0, 100.0), 0.0)


if __name__ == "__main__":
    unittest.main()
