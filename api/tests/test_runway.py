"""Unit and integration tests for Plutus runway math, batched balance tiers, and HTTP/MCP parity."""

from dataclasses import FrozenInstanceError
from datetime import date, timedelta
import json
from pathlib import Path
import sys
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Add api directory to sys.path so app can be imported
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base, get_db
from app.main import app
from app.models import Account, AssetSnapshot, BudgetRule, Loan, Transaction
from app.routers.mcp import _run_plutus_get_runway
from app.services.runway import (
    PERPETUAL_RUNWAY_WEEKS_SENTINEL,
    PERPETUAL_RUNWAY_YEARS_SENTINEL,
    calculate_compounding_runway,
    calculate_emergency_runway,
    calculate_time_banked,
    calculate_weekly_growth_rate,
    get_balance_tier_snapshot,
    get_runway_snapshot,
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


class TestRunwayTelemetryAndBatchedSnapshots(unittest.TestCase):
    """Integration tests for batched BalanceTierSnapshot, RunwaySnapshot, and HTTP/MCP parity."""

    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        Base.metadata.create_all(bind=self.engine)
        self.db = self.Session()

        def override_get_db():
            try:
                yield self.db
            finally:
                pass

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def _add_account(
        self,
        *,
        name: str,
        account_type: str,
        tier: str,
        currency: str = "NZD",
        is_active: bool = True,
    ) -> Account:
        acc = Account(
            name=name,
            institution="Test Bank",
            account_type=account_type,
            tier=tier,
            currency=currency,
            is_active=is_active,
        )
        self.db.add(acc)
        self.db.flush()
        return acc

    def _add_snapshot(
        self,
        *,
        account_id: int,
        snapshot_date: date,
        balance: float,
        balance_nzd: float,
        currency: str = "NZD",
    ) -> AssetSnapshot:
        snap = AssetSnapshot(
            account_id=account_id,
            snapshot_date=snapshot_date,
            balance=balance,
            currency=currency,
            exchange_rate=1.0,
            balance_nzd=balance_nzd,
            source="manual",
        )
        self.db.add(snap)
        self.db.flush()
        return snap

    def _seed_eight_plus_accounts(self, ref_date: date) -> dict[str, Account]:
        accounts = {
            "checking": self._add_account(name="Alex Checking 1001", account_type="checking", tier="cash"),
            "card": self._add_account(name="Apex Credit Card", account_type="credit_card", tier="cash"),
            "unknown": self._add_account(name="Mystery Bucket", account_type="savings", tier="reserve"),
            "growth_1": self._add_account(name="Global Index Fund", account_type="brokerage", tier="Growth"),
            "growth_2": self._add_account(name="BTC Vault", account_type="crypto", tier="GROWTH", currency="BTC"),
            "locked": self._add_account(name="KiwiSaver Locked", account_type="kiwisaver", tier="LOCKED"),
            "empty_cash": self._add_account(name="Zero Snap Vault", account_type="savings", tier="cash"),
            "extra_growth": self._add_account(name="Term Portfolio", account_type="brokerage", tier="growth"),
            "inactive": self._add_account(
                name="Closed Legacy", account_type="checking", tier="cash", is_active=False
            ),
        }
        prior = ref_date - timedelta(days=10)
        future = ref_date + timedelta(days=5)
        self._add_snapshot(account_id=accounts["checking"].id, snapshot_date=prior, balance=1000.0, balance_nzd=1000.0)
        self._add_snapshot(account_id=accounts["checking"].id, snapshot_date=ref_date, balance=4000.0, balance_nzd=4000.0)
        self._add_snapshot(account_id=accounts["checking"].id, snapshot_date=future, balance=9999.0, balance_nzd=9999.0)
        self._add_snapshot(account_id=accounts["card"].id, snapshot_date=ref_date, balance=-500.0, balance_nzd=-500.0)
        self._add_snapshot(account_id=accounts["unknown"].id, snapshot_date=ref_date, balance=1500.0, balance_nzd=1500.0)
        self._add_snapshot(account_id=accounts["growth_1"].id, snapshot_date=prior, balance=10000.0, balance_nzd=10000.0)
        self._add_snapshot(account_id=accounts["growth_1"].id, snapshot_date=ref_date, balance=20000.0, balance_nzd=20000.0)
        self._add_snapshot(account_id=accounts["growth_2"].id, snapshot_date=ref_date, balance=0.1, balance_nzd=12000.0, currency="BTC")
        self._add_snapshot(account_id=accounts["locked"].id, snapshot_date=ref_date, balance=30000.0, balance_nzd=30000.0)
        self._add_snapshot(account_id=accounts["extra_growth"].id, snapshot_date=ref_date, balance=5000.0, balance_nzd=5000.0)
        self._add_snapshot(account_id=accounts["inactive"].id, snapshot_date=ref_date, balance=90000.0, balance_nzd=90000.0)
        self.db.commit()
        return accounts

    def test_batched_balance_tier_snapshot_single_select_and_as_of_cutoff(self) -> None:
        ref_date = date(2026, 11, 20)
        self._seed_eight_plus_accounts(ref_date)

        selects: list[str] = []

        def capture_select(_conn, _cursor, statement, _params, _context, _executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                selects.append(statement)

        event.listen(self.engine, "before_cursor_execute", capture_select)
        try:
            snap_as_of = get_balance_tier_snapshot(self.db, as_of=ref_date)
        finally:
            event.remove(self.engine, "before_cursor_execute", capture_select)

        self.assertEqual(len(selects), 1)
        self.assertEqual(len(snap_as_of.accounts), 8)
        self.assertEqual(snap_as_of.cash_balance, 5000.0)
        self.assertEqual(snap_as_of.growth_balance, 37000.0)
        self.assertEqual(snap_as_of.locked_balance, 30000.0)
        self.assertEqual(snap_as_of.total_liquid_balance, 42000.0)
        self.assertEqual(
            snap_as_of.tier_labels.as_dict(),
            {"cash": "Cash Floor", "growth": "Growth", "locked": "KiwiSaver Locked"},
        )
        zero_snap_acct = next(a for a in snap_as_of.accounts if a.account_name == "Zero Snap Vault")
        self.assertIsNone(zero_snap_acct.snapshot_date)
        self.assertEqual(zero_snap_acct.balance, 0.0)
        self.assertEqual(zero_snap_acct.balance_nzd, 0.0)
        unknown_acct = next(a for a in snap_as_of.accounts if a.account_name == "Mystery Bucket")
        self.assertEqual(unknown_acct.tier, "cash")

        # Latest mode without as_of includes the future snapshot for checking (9999 instead of 4000)
        latest = get_balance_tier_snapshot(self.db)
        self.assertEqual(latest.cash_balance, 10999.0)

    def test_tier_labels_exclude_credit_card_for_single_cash_account(self) -> None:
        ref_date = date(2026, 11, 20)
        checking = self._add_account(name="Alex Checking 1001", account_type="checking", tier="cash")
        card = self._add_account(name="Apex Credit Card", account_type="credit_card", tier="cash")
        growth = self._add_account(name="Global Index Fund", account_type="brokerage", tier="growth")
        self._add_snapshot(account_id=checking.id, snapshot_date=ref_date, balance=5000.0, balance_nzd=5000.0)
        self._add_snapshot(account_id=card.id, snapshot_date=ref_date, balance=-250.0, balance_nzd=-250.0)
        self._add_snapshot(account_id=growth.id, snapshot_date=ref_date, balance=8000.0, balance_nzd=8000.0)
        self.db.commit()

        snap = get_balance_tier_snapshot(self.db, as_of=ref_date)
        self.assertEqual(
            snap.tier_labels.as_dict(),
            {"cash": "Alex Checking 1001", "growth": "Global Index Fund", "locked": "Locked Reserve"},
        )

    def test_snapshots_are_frozen_and_survive_db_close(self) -> None:
        ref_date = date(2026, 11, 20)
        checking = self._add_account(name="Alex Checking 1001", account_type="checking", tier="cash")
        self._add_snapshot(account_id=checking.id, snapshot_date=ref_date, balance=8000.0, balance_nzd=8000.0)
        self.db.commit()

        balances = get_balance_tier_snapshot(self.db, as_of=ref_date)
        runway = get_runway_snapshot(self.db, as_of=ref_date)
        self.db.close()

        self.assertEqual(balances.cash_balance, 8000.0)
        self.assertEqual(balances.accounts[0].account_name, "Alex Checking 1001")
        self.assertEqual(runway.balances.cash_balance, 8000.0)
        self.assertEqual(runway.tier_labels.cash, "Alex Checking 1001")

        with self.assertRaises(FrozenInstanceError):
            balances.cash_balance = 0.0  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            balances.accounts[0].balance_nzd = 0.0  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            balances.tier_labels.cash = "Mutated"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            runway.net_worth = 0.0  # type: ignore[misc]

    def test_observed_vs_effective_burn_fallback_behavior(self) -> None:
        ref_date = date(2026, 11, 18)
        checking = self._add_account(name="Alex Checking 1001", account_type="checking", tier="cash")
        self._add_snapshot(account_id=checking.id, snapshot_date=ref_date, balance=10000.0, balance_nzd=10000.0)
        self.db.commit()

        # Zero spend and zero budgets -> observed=0.0, effective=DEFAULT_WEEKLY_BURN_NZD (1000.0)
        default_snap = get_runway_snapshot(self.db, as_of=ref_date)
        self.assertEqual(default_snap.observed_weekly_burn, 0.0)
        self.assertEqual(default_snap.effective_weekly_burn, 1000.0)

        # Active budget target without spend -> observed=0.0, effective=weekly_budget_target (400.0)
        self.db.add(
            BudgetRule(
                category="Groceries",
                weekly_limit=400.0,
                monthly_limit=1733.33,
                budget_period="weekly",
                is_active=True,
            )
        )
        self.db.commit()
        budget_snap = get_runway_snapshot(self.db, as_of=ref_date)
        self.assertEqual(budget_snap.observed_weekly_burn, 0.0)
        self.assertEqual(budget_snap.effective_weekly_burn, 400.0)

        # Positive spend -> observed_weekly_burn == effective_weekly_burn
        self.db.add(
            Transaction(
                transaction_date=ref_date,
                account_id=checking.id,
                amount=-200.0,
                currency="NZD",
                merchant="Fresh Market",
                category="Groceries",
                raw_hash="burn-test-tx-1",
            )
        )
        self.db.commit()
        observed_snap = get_runway_snapshot(self.db, as_of=ref_date)
        self.assertEqual(observed_snap.observed_weekly_burn, 50.0)
        self.assertEqual(observed_snap.effective_weekly_burn, 50.0)

    def test_liabilities_net_worth_and_http_mcp_parity_finite_and_perpetual(self) -> None:
        today = date.today()
        checking = self._add_account(name="Alex Checking 1001", account_type="checking", tier="cash")
        growth = self._add_account(name="Global Index Fund", account_type="brokerage", tier="growth")
        locked = self._add_account(name="KiwiSaver Locked", account_type="kiwisaver", tier="locked")
        self._add_snapshot(account_id=checking.id, snapshot_date=today, balance=10000.0, balance_nzd=10000.0)
        self._add_snapshot(account_id=growth.id, snapshot_date=today, balance=20000.0, balance_nzd=20000.0)
        self._add_snapshot(account_id=locked.id, snapshot_date=today, balance=15000.0, balance_nzd=15000.0)
        self.db.add(
            Loan(
                name="Auto Loan",
                lender="Civic Finance",
                loan_type="borrowed",
                principal_amount=5000.0,
                annual_interest_rate=0.05,
                term_months=24,
                monthly_payment=220.0,
                start_date=today - timedelta(days=30),
                is_active=True,
            )
        )
        self.db.add(
            Transaction(
                transaction_date=today,
                account_id=checking.id,
                amount=-800.0,
                currency="NZD",
                merchant="Corner Mart",
                category="Groceries",
                raw_hash="parity-tx-1",
            )
        )
        self.db.commit()

        http_res = self.client.get(f"/runway?as_of={today.isoformat()}")
        self.assertEqual(http_res.status_code, 200)
        http_body = http_res.json()
        mcp_body = json.loads(_run_plutus_get_runway(db=self.db, reference_date=today))

        self.assertFalse(http_body["is_perpetual"])
        self.assertFalse(mcp_body["is_perpetual"])
        self.assertEqual(http_body["primary_runway_weeks"], mcp_body["primary_runway_weeks"])
        self.assertEqual(http_body["primary_runway_years"], mcp_body["primary_runway_years"])
        self.assertEqual(http_body["time_banked_this_week"], mcp_body["time_banked_this_week"])
        self.assertEqual(http_body["weekly_spend_this_week"], mcp_body["weekly_spend_this_week_nzd"])
        self.assertEqual(http_body["weekly_budget_target"], mcp_body["weekly_budget_target_nzd"])
        self.assertEqual(http_body["breakdown"]["total_liabilities"], 5000.0)
        self.assertEqual(http_body["breakdown"]["net_worth"], 40000.0)
        self.assertEqual(mcp_body["breakdown"]["total_liabilities_nzd"], 5000.0)
        self.assertEqual(mcp_body["breakdown"]["net_worth_nzd"], 40000.0)
        self.assertEqual(
            http_body["breakdown"]["weekly_burn_rate"], mcp_body["effective_weekly_burn_nzd"]
        )
        self.assertEqual(
            http_body["breakdown"]["variable_weekly_burn"],
            mcp_body["breakdown"]["variable_weekly_burn_nzd"],
        )
        self.assertEqual(
            http_body["breakdown"]["recurring_weekly_burn"],
            mcp_body["breakdown"]["recurring_weekly_burn_nzd"],
        )
        self.assertEqual(
            http_body["breakdown"]["this_week_variable_spend"],
            mcp_body["breakdown"]["this_week_variable_spend_nzd"],
        )
        self.assertEqual(http_body["breakdown"]["locked_weeks"], mcp_body["breakdown"]["locked_weeks"])
        self.assertEqual(http_body["breakdown"]["growth_weeks"], mcp_body["breakdown"]["growth_weeks"])
        self.assertEqual(http_body["breakdown"]["tier_labels"], mcp_body["breakdown"]["tier_labels"])

        # Raise growth balance to trigger Perpetual Freedom and verify sentinel + MCP string parity
        self._add_snapshot(
            account_id=growth.id,
            snapshot_date=today + timedelta(days=1),
            balance=2000000.0,
            balance_nzd=2000000.0,
        )
        self.db.commit()

        perp_http = self.client.get("/runway").json()
        perp_mcp = json.loads(_run_plutus_get_runway(db=self.db))
        self.assertTrue(perp_http["is_perpetual"])
        self.assertTrue(perp_mcp["is_perpetual"])
        self.assertIsNone(perp_http["breakdown"]["growth_weeks"])
        self.assertIsNone(perp_mcp["breakdown"]["growth_weeks"])
        self.assertEqual(perp_http["primary_runway_weeks"], PERPETUAL_RUNWAY_WEEKS_SENTINEL)
        self.assertEqual(perp_http["primary_runway_years"], PERPETUAL_RUNWAY_YEARS_SENTINEL)
        self.assertEqual(perp_mcp["primary_runway_weeks"], "Infinite (Perpetual Freedom)")
        self.assertEqual(perp_mcp["primary_runway_years"], "Infinite")


if __name__ == "__main__":
    unittest.main()
