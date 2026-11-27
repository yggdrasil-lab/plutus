"""Tests for revolving credit limits, credit_line account type, FX fail-loud rules, and Runway/MCP credit backstop."""

from dataclasses import FrozenInstanceError
from datetime import date, timedelta
import json
from pathlib import Path
import sys
import unittest

from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base, get_db
from app.main import app
from app.models import Account, AssetSnapshot, BudgetRule, Loan
from app.routers.mcp import _run_plutus_get_runway
from app.services.credit_telemetry import CreditFxResolutionError
from app.services.fx_service import cache_exchange_rate
from app.services.runway import (
    CreditBackstopSnapshot,
    get_balance_tier_snapshot,
    get_runway_snapshot,
)


class TestCreditLimitAndEmergencyBackstop(unittest.TestCase):
    """Integration and unit tests for Slice 01 credit limit telemetry and emergency backstop."""

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

    def _add_snapshot(
        self,
        *,
        account_id: int,
        snapshot_date: date,
        balance: float,
        currency: str = "NZD",
        exchange_rate: float = 1.0,
    ) -> AssetSnapshot:
        snap = AssetSnapshot(
            account_id=account_id,
            snapshot_date=snapshot_date,
            balance=balance,
            currency=currency,
            exchange_rate=exchange_rate,
            balance_nzd=round(balance * exchange_rate, 2),
            source="manual",
        )
        self.db.add(snap)
        self.db.flush()
        return snap

    def test_account_credit_limit_crud_validation_clear_and_hydration(self) -> None:
        # Create credit_line with limit and verify hydrated AccountRead (including no-snapshot state)
        create_res = self.client.post(
            "/accounts",
            json={
                "name": "NZ Synthetic Line",
                "account_type": "credit_line",
                "tier": "cash",
                "institution": "North Bank",
                "currency": "NZD",
                "credit_limit": 10000.0,
            },
        )
        self.assertEqual(create_res.status_code, 201)
        created = create_res.json()
        self.assertEqual(created["account_type"], "credit_line")
        self.assertEqual(created["credit_limit"], 10000.0)
        self.assertEqual(created["credit_limit_nzd"], 10000.0)
        self.assertEqual(created["credit_owed_native"], 0.0)
        self.assertEqual(created["credit_owed_nzd"], 0.0)
        self.assertEqual(created["available_credit_native"], 10000.0)
        self.assertEqual(created["available_credit_nzd"], 10000.0)
        self.assertEqual(created["credit_utilization_pct"], 0.0)

        # Reject zero and negative credit_limit on create and patch
        for bad_val in (0.0, -500.0):
            bad_create = self.client.post(
                "/accounts",
                json={
                    "name": "Bad Limit Card",
                    "account_type": "credit_card",
                    "tier": "cash",
                    "institution": "North Bank",
                    "currency": "NZD",
                    "credit_limit": bad_val,
                },
            )
            self.assertEqual(bad_create.status_code, 422)

            bad_patch = self.client.patch(
                f"/accounts/{created['id']}",
                json={"credit_limit": bad_val},
            )
            self.assertEqual(bad_patch.status_code, 422)

        # Record outstanding debt snapshot (-2500) and verify derived telemetry
        today = date(2026, 11, 27)
        self._add_snapshot(
            account_id=created["id"],
            snapshot_date=today - timedelta(days=2),
            balance=-1000.0,
        )
        self._add_snapshot(
            account_id=created["id"],
            snapshot_date=today,
            balance=-2500.0,
        )
        self.db.commit()

        # PATCH omitting credit_limit preserves existing limit and hydrates latest balance
        rename_res = self.client.patch(
            f"/accounts/{created['id']}",
            json={"name": "NZ Synthetic Cashline"},
        )
        self.assertEqual(rename_res.status_code, 200)
        renamed = rename_res.json()
        self.assertEqual(renamed["credit_limit"], 10000.0)
        self.assertEqual(renamed["credit_owed_native"], 2500.0)
        self.assertEqual(renamed["available_credit_native"], 7500.0)
        self.assertEqual(renamed["credit_utilization_pct"], 25.0)

        # Overpayment (CR positive balance) yields 0 owed and 100% available
        self._add_snapshot(
            account_id=created["id"],
            snapshot_date=today + timedelta(days=1),
            balance=300.0,
        )
        self.db.commit()
        cr_get = self.client.get(f"/accounts/{created['id']}").json()
        self.assertEqual(cr_get["credit_owed_native"], 0.0)
        self.assertEqual(cr_get["available_credit_native"], 10000.0)
        self.assertEqual(cr_get["credit_utilization_pct"], 0.0)

        # Over-limit balance clamps available_credit at 0.0 and surfaces >100% utilization
        self._add_snapshot(
            account_id=created["id"],
            snapshot_date=today + timedelta(days=2),
            balance=-11000.0,
        )
        self.db.commit()
        over_get = self.client.get(f"/accounts/{created['id']}").json()
        self.assertEqual(over_get["credit_owed_native"], 11000.0)
        self.assertEqual(over_get["available_credit_native"], 0.0)
        self.assertEqual(over_get["credit_utilization_pct"], 110.0)

        # Explicit null in PATCH clears credit_limit and all derived credit fields
        clear_res = self.client.patch(
            f"/accounts/{created['id']}",
            json={"credit_limit": None},
        )
        self.assertEqual(clear_res.status_code, 200)
        cleared = clear_res.json()
        self.assertIsNone(cleared["credit_limit"])
        self.assertIsNone(cleared["credit_limit_nzd"])
        self.assertIsNone(cleared["credit_owed_native"])
        self.assertIsNone(cleared["credit_owed_nzd"])
        self.assertIsNone(cleared["available_credit_native"])
        self.assertIsNone(cleared["available_credit_nzd"])
        self.assertIsNone(cleared["credit_utilization_pct"])

    def test_non_nzd_credit_fx_resolution_and_fail_loud_guard(self) -> None:
        # Missing FX rate fails loud (never silently returns 1.0)
        sg_line = Account(
            name="SG Synthetic Cashline",
            account_type="credit_line",
            tier="cash",
            institution="DBS Bank",
            currency="SGD",
            credit_limit=10000.0,
            is_active=True,
        )
        self.db.add(sg_line)
        self.db.commit()

        list_fail = self.client.get("/accounts")
        self.assertEqual(list_fail.status_code, 422)
        self.assertIn("SGD->NZD", list_fail.json()["detail"])

        runway_fail = self.client.get("/runway")
        self.assertEqual(runway_fail.status_code, 422)

        with self.assertRaises(CreditFxResolutionError):
            get_balance_tier_snapshot(self.db)

        with self.assertRaises(ToolError):
            _run_plutus_get_runway(db=self.db)

        # Non-default snapshot FX rate resolves cleanly when ExchangeRate cache is empty
        today = date(2026, 11, 27)
        self._add_snapshot(
            account_id=sg_line.id,
            snapshot_date=today,
            balance=-1000.0,
            currency="SGD",
            exchange_rate=1.30,
        )
        self.db.commit()

        from_snap = self.client.get(f"/accounts/{sg_line.id}")
        self.assertEqual(from_snap.status_code, 200)
        body_snap = from_snap.json()
        self.assertEqual(body_snap["credit_limit_nzd"], 13000.0)
        self.assertEqual(body_snap["credit_owed_nzd"], 1300.0)
        self.assertEqual(body_snap["available_credit_nzd"], 11700.0)

        # Cached ExchangeRate takes precedence over snapshot fallback
        cache_exchange_rate(
            self.db,
            rate_date=today,
            from_currency="SGD",
            to_currency="NZD",
            rate=1.35,
            source="test_stub",
        )
        from_cache = self.client.get(f"/accounts/{sg_line.id}").json()
        self.assertEqual(from_cache["credit_limit_nzd"], 13500.0)
        self.assertEqual(from_cache["credit_owed_nzd"], 1350.0)
        self.assertEqual(from_cache["available_credit_nzd"], 12150.0)

    def test_runway_purity_credit_backstop_tier_labels_and_http_mcp_parity(self) -> None:
        today = date.today()
        cache_exchange_rate(
            self.db,
            rate_date=today,
            from_currency="SGD",
            to_currency="NZD",
            rate=1.30,
            source="test_stub",
        )
        checking = Account(
            name="Alex Checking 1001",
            account_type="checking",
            tier="cash",
            institution="North Bank",
            currency="NZD",
            is_active=True,
        )
        nz_card = Account(
            name="NZ Synthetic Visa",
            account_type="credit_card",
            tier="cash",
            institution="North Bank",
            currency="NZD",
            credit_limit=5000.0,
            is_active=True,
        )
        sg_line = Account(
            name="SG Synthetic Cashline",
            account_type="credit_line",
            tier="cash",
            institution="DBS Bank",
            currency="SGD",
            credit_limit=10000.0,
            is_active=True,
        )
        inactive_line = Account(
            name="Closed Synthetic Line",
            account_type="credit_line",
            tier="cash",
            institution="DBS Bank",
            currency="NZD",
            credit_limit=20000.0,
            is_active=False,
        )
        growth = Account(
            name="Global Index Fund",
            account_type="brokerage",
            tier="growth",
            institution="Broker",
            currency="NZD",
            is_active=True,
        )
        locked = Account(
            name="KiwiSaver Core",
            account_type="kiwisaver",
            tier="locked",
            institution="Simplicity",
            currency="NZD",
            is_active=True,
        )
        self.db.add_all([checking, nz_card, sg_line, inactive_line, growth, locked])
        self.db.flush()

        self._add_snapshot(account_id=checking.id, snapshot_date=today, balance=10000.0)
        self._add_snapshot(account_id=nz_card.id, snapshot_date=today, balance=-1500.0)
        self._add_snapshot(
            account_id=sg_line.id,
            snapshot_date=today,
            balance=-1000.0,
            currency="SGD",
            exchange_rate=1.30,
        )
        self._add_snapshot(account_id=growth.id, snapshot_date=today, balance=20000.0)
        self._add_snapshot(account_id=locked.id, snapshot_date=today, balance=15000.0)
        self.db.add(
            BudgetRule(
                category="Living",
                weekly_limit=1000.0,
                monthly_limit=4333.33,
                is_active=True,
            )
        )
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
        self.db.commit()

        # Verify single-SELECT batched query for NZD-only or un-cached? Wait: here SGD triggers 1 FX lookup,
        # while BalanceTierSnapshot itself remains 1 batched account+snapshot SELECT.
        snap = get_runway_snapshot(self.db, as_of=today)
        self.assertIsInstance(snap.credit_backstop, CreditBackstopSnapshot)
        with self.assertRaises(FrozenInstanceError):
            snap.credit_backstop.backstop_weeks = 99.0  # type: ignore[misc]

        # Purity invariant: cash_balance = 10000 - 1500 - 1300 = 7200; total_liabilities = 5000 Loan only
        self.assertEqual(snap.cash_balance, 7200.0)
        self.assertEqual(snap.growth_balance, 20000.0)
        self.assertEqual(snap.locked_balance, 15000.0)
        self.assertEqual(snap.total_liquid_balance, 27200.0)
        self.assertEqual(snap.total_liabilities, 5000.0)
        self.assertEqual(snap.net_worth, 37200.0)
        self.assertEqual(snap.emergency_weeks, 7.2)

        # Tier labels exclude both credit_card and credit_line when naming the single cash account
        self.assertEqual(snap.tier_labels.cash, "Alex Checking 1001")

        # Credit backstop aggregates NZD card (5000 limit, 1500 owed, 3500 avail)
        # + SGD line @ 1.30 (13000 limit, 1300 owed, 11700 avail)
        cb = snap.credit_backstop
        self.assertEqual(cb.account_count, 2)
        self.assertEqual(cb.total_credit_limit_nzd, 18000.0)
        self.assertEqual(cb.total_credit_owed_nzd, 2800.0)
        self.assertEqual(cb.total_available_credit_nzd, 15200.0)
        self.assertEqual(cb.credit_card_available_nzd, 3500.0)
        self.assertEqual(cb.credit_line_available_nzd, 11700.0)
        self.assertEqual(cb.credit_utilization_pct, 15.56)
        self.assertEqual(cb.backstop_weeks, 15.2)

        # HTTP GET /runway and MCP _run_plutus_get_runway parity
        http_body = self.client.get(f"/runway?as_of={today.isoformat()}").json()
        mcp_body = json.loads(_run_plutus_get_runway(db=self.db, reference_date=today))
        self.assertEqual(http_body["breakdown"]["credit_backstop"], cb.as_dict())
        self.assertEqual(mcp_body["breakdown"]["credit_backstop"], cb.as_dict())

    def test_nzd_credit_accounts_keep_single_select_invariant_in_balance_tier_snapshot(self) -> None:
        today = date(2026, 11, 27)
        checking = Account(
            name="Alex Checking 1001",
            account_type="checking",
            tier="cash",
            institution="North Bank",
            currency="NZD",
            is_active=True,
        )
        card = Account(
            name="NZ Synthetic Visa",
            account_type="credit_card",
            tier="cash",
            institution="North Bank",
            currency="NZD",
            credit_limit=4000.0,
            is_active=True,
        )
        self.db.add_all([checking, card])
        self.db.flush()
        self._add_snapshot(account_id=checking.id, snapshot_date=today, balance=6000.0)
        self._add_snapshot(account_id=card.id, snapshot_date=today, balance=-500.0)
        self.db.commit()

        selects: list[str] = []

        def capture_select(_conn, _cursor, statement, _params, _context, _executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                selects.append(statement)

        event.listen(self.engine, "before_cursor_execute", capture_select)
        try:
            snap = get_balance_tier_snapshot(self.db, as_of=today)
        finally:
            event.remove(self.engine, "before_cursor_execute", capture_select)

        self.assertEqual(len(selects), 1)
        self.assertEqual(snap.credit_backstop.total_available_credit_nzd, 3500.0)


if __name__ == "__main__":
    unittest.main()
