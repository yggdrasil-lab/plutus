"""Unit tests for Plutus burn_service, Wise FX guard, FX repair, and average-burn budgets."""

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
from app.models import Account, AssetSnapshot, BudgetRule, Transaction
from app.services.burn_service import (
    calculate_category_average_burn,
    calculate_weekly_burn_summary,
    repair_transaction_fx_rates,
    to_nzd_amount,
)
from app.services.connectors.wise import normalize_wise_feed


class StubFxFetcher:
    """In-memory FX fetcher for offline unit tests."""

    def __init__(self, rates: dict[tuple[str, str], float]) -> None:
        self._rates = rates

    def fetch_rate(self, *, rate_date: date, from_currency: str, to_currency: str):
        rate = self._rates.get((from_currency.upper(), to_currency.upper()))
        return (rate, "stub") if rate is not None else None


class TestBurnService(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        Base.metadata.create_all(bind=self.engine)
        self.db = self.Session()

        def override_get_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

        self.acc = Account(
            name="Wise NZD",
            institution="Wise",
            account_type="checking",
            tier="cash",
            currency="NZD",
        )
        self.sgd_acc = Account(
            name="DBS Multiplier",
            institution="DBS Bank",
            account_type="savings",
            tier="cash",
            currency="SGD",
        )
        self.db.add_all([self.acc, self.sgd_acc])
        self.db.commit()
        self.db.refresh(self.acc)
        self.db.refresh(self.sgd_acc)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_to_nzd_amount_never_multiplies_nzd_by_foreign_rate(self):
        # Corrupted Wise NZD->PHP rate must never inflate an NZD spend amount
        self.assertAlmostEqual(to_nzd_amount(-558.08, "NZD", 36.0647), 558.08, places=2)
        self.assertAlmostEqual(to_nzd_amount(-100.0, "SGD", 1.32), 132.0, places=2)
        self.assertAlmostEqual(to_nzd_amount(-50.0, "SGD", None), 50.0, places=2)

    def test_wise_normalizer_guards_non_nzd_target_exchange_rates(self):
        # 1. NZD balance sending PHP: must NOT keep 36.0647 as exchange_rate
        nzd_feed = normalize_wise_feed(
            profile={"id": 1, "type": "PERSONAL"},
            balance={"id": 10, "currency": "NZD", "type": "STANDARD", "amount": {"value": 100.0}},
            raw_transactions=[
                {
                    "referenceNumber": "TX-NZD-PHP",
                    "date": "2026-10-21T10:00:00Z",
                    "amount": {"value": -558.08, "currency": "NZD"},
                    "exchangeDetails": {
                        "rate": 36.0647,
                        "toAmount": {"value": 20000.0, "currency": "PHP"},
                    },
                    "details": {"description": "Sent money to Rowena"},
                }
            ],
        )
        self.assertIsNone(nzd_feed.transactions[0].exchange_rate)

        # 2. SGD balance buying AUD on card vs converting SGD -> NZD
        sgd_feed = normalize_wise_feed(
            profile={"id": 1, "type": "PERSONAL"},
            balance={"id": 11, "currency": "SGD", "type": "STANDARD", "amount": {"value": 500.0}},
            raw_transactions=[
                {
                    "referenceNumber": "TX-SGD-AUD",
                    "date": "2026-11-11T10:00:00Z",
                    "amount": {"value": -217.54, "currency": "SGD"},
                    "exchangeDetails": {
                        "rate": 1.1358,
                        "toAmount": {"value": 247.08, "currency": "AUD"},
                    },
                    "details": {"description": "Amazon Marketplace Au"},
                },
                {
                    "referenceNumber": "TX-SGD-NZD",
                    "date": "2026-11-06T10:00:00Z",
                    "amount": {"value": -711.61, "currency": "SGD"},
                    "exchangeDetails": {
                        "rate": 1.40821,
                        "toAmount": {"value": 1000.0, "currency": "NZD"},
                    },
                    "details": {"description": "Converted SGD to NZD"},
                },
            ],
        )
        self.assertIsNone(sgd_feed.transactions[0].exchange_rate)
        self.assertAlmostEqual(sgd_feed.transactions[1].exchange_rate, 1.40821, places=5)

    def test_repair_transaction_fx_rates_fixes_corrupted_and_missing_rows(self):
        today = date(2026, 11, 18)
        nzd_corrupted = Transaction(
            transaction_date=today - timedelta(days=5),
            account_id=self.acc.id,
            amount=-558.08,
            currency="NZD",
            exchange_rate=36.0647,
            merchant="Remittance",
            category="General",
            raw_hash="fx-repair-nzd",
        )
        sgd_missing = Transaction(
            transaction_date=today - timedelta(days=4),
            account_id=self.sgd_acc.id,
            amount=-100.0,
            currency="SGD",
            exchange_rate=None,
            merchant="DBS Spend",
            category="Food",
            raw_hash="fx-repair-sgd-null",
        )
        sgd_aud_leaked = Transaction(
            transaction_date=today - timedelta(days=3),
            account_id=self.sgd_acc.id,
            amount=-200.0,
            currency="SGD",
            exchange_rate=1.14,
            merchant="Amazon AU",
            category="General",
            raw_hash="fx-repair-sgd-aud",
        )
        paired_transfer = Transaction(
            transaction_date=today - timedelta(days=2),
            account_id=self.acc.id,
            amount=-896.30,
            currency="NZD",
            exchange_rate=0.73039,
            merchant="Transfer to Wise SGD",
            category="Transfers",
            linked_transaction_id=999,
            raw_hash="fx-repair-paired",
        )
        self.db.add_all([nzd_corrupted, sgd_missing, sgd_aud_leaked, paired_transfer])
        self.db.commit()

        repaired = repair_transaction_fx_rates(
            self.db,
            fetcher=StubFxFetcher({("SGD", "NZD"): 1.32}),
        )
        self.assertEqual(repaired, 3)

        self.db.refresh(nzd_corrupted)
        self.db.refresh(sgd_missing)
        self.db.refresh(sgd_aud_leaked)
        self.db.refresh(paired_transfer)

        self.assertEqual(float(nzd_corrupted.exchange_rate), 1.0)
        self.assertAlmostEqual(float(sgd_missing.exchange_rate), 1.32, places=2)
        self.assertAlmostEqual(float(sgd_aud_leaked.exchange_rate), 1.32, places=2)
        self.assertAlmostEqual(float(paired_transfer.exchange_rate), 0.73039, places=5)

    def test_weekly_burn_summary_average_burn_and_budget_sync(self):
        today = date.today()
        monday = today - timedelta(days=today.weekday())

        self.db.add(
            AssetSnapshot(
                account_id=self.acc.id,
                snapshot_date=today,
                balance=50000.0,
                currency="NZD",
                exchange_rate=1.0,
                balance_nzd=50000.0,
                source="manual",
            )
        )
        self.db.add_all(
            [
                # Recurring rent: $400/wk
                Transaction(
                    transaction_date=monday,
                    account_id=self.acc.id,
                    amount=-400.0,
                    currency="NZD",
                    merchant="Home rent",
                    category="Housing",
                    recurrence="weekly",
                    raw_hash="burn-rent-1",
                ),
                # Variable groceries inside current week & 4w window: $200 NZD
                Transaction(
                    transaction_date=monday,
                    account_id=self.acc.id,
                    amount=-200.0,
                    currency="NZD",
                    merchant="Pak n Save",
                    category="Groceries",
                    recurrence=None,
                    raw_hash="burn-groc-1",
                ),
                # Variable SGD food 10 days ago at 1.30 -> $130 NZD
                Transaction(
                    transaction_date=today - timedelta(days=10),
                    account_id=self.sgd_acc.id,
                    amount=-100.0,
                    currency="SGD",
                    exchange_rate=1.30,
                    merchant="Hawker",
                    category="Food",
                    recurrence=None,
                    raw_hash="burn-food-sgd",
                ),
                # Corrupted NZD rate on 20 days ago -> must count as $100 NZD, not $3600!
                Transaction(
                    transaction_date=today - timedelta(days=20),
                    account_id=self.acc.id,
                    amount=-100.0,
                    currency="NZD",
                    exchange_rate=36.0,
                    merchant="Remittance",
                    category="General",
                    recurrence=None,
                    raw_hash="burn-nzd-corrupt",
                ),
                # One-off purchase -> excluded from burn
                Transaction(
                    transaction_date=today - timedelta(days=3),
                    account_id=self.acc.id,
                    amount=-8000.0,
                    currency="NZD",
                    merchant="Lexus Deposit",
                    category="General",
                    recurrence="one-off",
                    raw_hash="burn-oneoff",
                ),
            ]
        )
        self.db.commit()

        # 4w variable = (200 + 130 + 100) / 4 = 107.50; recurring = 400.00 -> effective = 507.50
        summary = calculate_weekly_burn_summary(self.db, today, lookback_weeks=4)
        self.assertAlmostEqual(summary.variable_weekly_burn, 107.50, places=2)
        self.assertAlmostEqual(summary.recurring_weekly_burn, 400.00, places=2)
        self.assertAlmostEqual(summary.effective_weekly_burn, 507.50, places=2)
        self.assertAlmostEqual(summary.this_week_variable_spend, 200.00, places=2)
        self.assertAlmostEqual(summary.this_week_spend, 600.00, places=2)

        # GET /runway exposes decomposed burn fields and matching weekly_spend_this_week
        runway_res = self.client.get("/runway")
        self.assertEqual(runway_res.status_code, 200)
        r_body = runway_res.json()
        self.assertAlmostEqual(r_body["weekly_spend_this_week"], 600.00, places=2)
        self.assertAlmostEqual(r_body["breakdown"]["weekly_burn_rate"], 507.50, places=2)
        self.assertAlmostEqual(r_body["breakdown"]["variable_weekly_burn"], 107.50, places=2)
        self.assertAlmostEqual(r_body["breakdown"]["recurring_weekly_burn"], 400.00, places=2)

        # GET /budgets/average-burn?lookback_weeks=4
        avg_res = self.client.get("/budgets/average-burn?lookback_weeks=4")
        self.assertEqual(avg_res.status_code, 200)
        avg_body = avg_res.json()
        self.assertAlmostEqual(avg_body["effective_weekly_burn"], 507.50, places=2)
        cats = {c["category"]: c for c in avg_body["categories"]}
        self.assertAlmostEqual(cats["Housing"]["average_weekly_burn"], 400.00, places=2)
        self.assertAlmostEqual(cats["Groceries"]["average_weekly_burn"], 50.00, places=2)
        self.assertAlmostEqual(cats["Food"]["average_weekly_burn"], 32.50, places=2)
        self.assertAlmostEqual(cats["General"]["average_weekly_burn"], 25.00, places=2)

        # Category percentages: variable total = 107.50 (Groceries 46.5%, Food 30.2%, General 23.3%, Housing 0%)
        self.assertAlmostEqual(cats["Housing"]["variable_percentage"], 0.0, places=1)
        self.assertAlmostEqual(cats["Groceries"]["variable_percentage"], 46.5, places=1)
        self.assertAlmostEqual(cats["Food"]["variable_percentage"], 30.2, places=1)
        self.assertAlmostEqual(cats["General"]["variable_percentage"], 23.3, places=1)
        self.assertAlmostEqual(cats["Housing"]["total_percentage"], 78.8, places=1)

        # Itemized recurring fixed commitments
        commitments = avg_body["recurring_commitments"]
        self.assertEqual(len(commitments), 1)
        self.assertEqual(commitments[0]["merchant"], "Home rent")
        self.assertEqual(commitments[0]["category"], "Housing")
        self.assertEqual(commitments[0]["recurrence"], "weekly")
        self.assertAlmostEqual(commitments[0]["weekly_nzd"], 400.00, places=2)
        self.assertAlmostEqual(commitments[0]["percentage_of_recurring"], 100.0, places=1)

        # POST /budgets/sync-from-burn?lookback_weeks=4 creates BudgetRule entries
        sync_res = self.client.post("/budgets/sync-from-burn?lookback_weeks=4")
        self.assertEqual(sync_res.status_code, 200)
        rules = {r["category"]: r for r in sync_res.json()}
        self.assertAlmostEqual(rules["Housing"]["weekly_limit"], 400.00, places=2)
        self.assertAlmostEqual(rules["Groceries"]["weekly_limit"], 50.00, places=2)
        self.assertAlmostEqual(rules["Food"]["weekly_limit"], 32.50, places=2)

        # Re-check /budgets/breakdown uses NZD FX across month period too
        month_res = self.client.get("/budgets/breakdown?period=month")
        self.assertEqual(month_res.status_code, 200)
        estimates = calculate_category_average_burn(self.db, today, lookback_weeks=4)
        self.assertEqual(estimates[0].category, "Housing")

    def test_annual_budget_category_amortization_without_hardcoding(self):
        """Any category marked budget_period='annual' spreads 365d spend across 52 weeks."""
        today = date.today()

        # Configure two different categories as annual budgets (no hardcoded category check)
        res_travel = self.client.post(
            "/budgets",
            json={"category": "Travel", "annual_limit": 2600.0, "budget_period": "annual"},
        )
        self.assertEqual(res_travel.status_code, 200)
        self.assertEqual(res_travel.json()["budget_period"], "annual")
        self.assertAlmostEqual(res_travel.json()["weekly_limit"], 50.0, places=2)
        self.assertAlmostEqual(res_travel.json()["annual_limit"], 2600.0, places=2)

        res_edu = self.client.post(
            "/budgets",
            json={"category": "Education", "annual_limit": 1040.0, "budget_period": "annual"},
        )
        self.assertEqual(res_edu.status_code, 200)

        self.db.add_all(
            [
                # Booking today in current week: must amortize to $50/wk, not $2,600/wk
                Transaction(
                    transaction_date=today,
                    account_id=self.acc.id,
                    amount=-2600.0,
                    currency="NZD",
                    merchant="Air New Zealand",
                    category="Travel",
                    recurrence=None,
                    raw_hash="annual-travel-today",
                ),
                # Charge 120 days ago (outside 4w & 12w windows, inside 365d window): $520 -> $10/wk
                Transaction(
                    transaction_date=today - timedelta(days=120),
                    account_id=self.acc.id,
                    amount=-520.0,
                    currency="NZD",
                    merchant="Certification Exam",
                    category="Education",
                    recurrence=None,
                    raw_hash="annual-edu-120d",
                ),
            ]
        )
        self.db.commit()

        summary = calculate_weekly_burn_summary(self.db, today, lookback_weeks=4)
        self.assertAlmostEqual(summary.variable_weekly_burn, 60.00, places=2)
        self.assertAlmostEqual(summary.this_week_variable_spend, 60.00, places=2)
        self.assertAlmostEqual(summary.effective_weekly_burn, 60.00, places=2)

        avg_res = self.client.get("/budgets/average-burn?lookback_weeks=4")
        self.assertEqual(avg_res.status_code, 200)
        cats = {c["category"]: c for c in avg_res.json()["categories"]}
        self.assertEqual(cats["Travel"]["budget_period"], "annual")
        self.assertAlmostEqual(cats["Travel"]["variable_weekly_burn"], 50.00, places=2)
        self.assertAlmostEqual(cats["Travel"]["suggested_annual_limit"], 2600.00, places=2)
        self.assertEqual(cats["Education"]["budget_period"], "annual")
        self.assertAlmostEqual(cats["Education"]["variable_weekly_burn"], 10.00, places=2)
        self.assertAlmostEqual(cats["Education"]["suggested_annual_limit"], 520.00, places=2)

        # Breakdown evaluates against annual limit (2600 / 2600 = 100%, not over budget)
        week_res = self.client.get("/budgets/breakdown?period=week")
        self.assertEqual(week_res.status_code, 200)
        w_body = week_res.json()
        self.assertAlmostEqual(w_body["total_spend"], 60.00, places=2)
        w_cats = {c["category"]: c for c in w_body["categories"]}
        self.assertEqual(w_cats["Travel"]["budget_period"], "annual")
        self.assertAlmostEqual(w_cats["Travel"]["budget_limit"], 2600.00, places=2)
        self.assertAlmostEqual(w_cats["Travel"]["total_spend"], 2600.00, places=2)
        self.assertFalse(w_cats["Travel"]["is_over_budget"])
        self.assertEqual(w_cats["Education"]["budget_period"], "annual")
        self.assertEqual(w_cats["Education"]["transaction_count"], 1)
        self.assertAlmostEqual(w_cats["Education"]["budget_limit"], 1040.00, places=2)
        self.assertAlmostEqual(w_cats["Education"]["total_spend"], 520.00, places=2)
        self.assertFalse(w_cats["Education"]["is_over_budget"])

        # Annual period query returns trailing 365d transactions even though outside current week
        edu_txs = self.client.get("/transactions?category=Education&period=annual")
        self.assertEqual(edu_txs.status_code, 200)
        self.assertEqual(len(edu_txs.json()), 1)
        self.assertEqual(edu_txs.json()[0]["merchant"], "Certification Exam")

    def test_tag_spending_summary_and_tag_filter(self):
        """Tag summary aggregates NZD spend and per-category breakdown across categories."""
        today = date.today()
        trip_tag = json.dumps(["japan-trip"])
        self.db.add_all(
            [
                Transaction(
                    transaction_date=today - timedelta(days=50),
                    account_id=self.acc.id,
                    amount=-1200.0,
                    currency="NZD",
                    merchant="ANA Flights",
                    category="Travel",
                    tags=trip_tag,
                    raw_hash="tag-japan-flight",
                ),
                Transaction(
                    transaction_date=today - timedelta(days=10),
                    account_id=self.sgd_acc.id,
                    amount=-100.0,
                    currency="SGD",
                    exchange_rate=1.30,
                    merchant="Ichiran Ramen",
                    category="Food",
                    tags=trip_tag,
                    raw_hash="tag-japan-food",
                ),
                Transaction(
                    transaction_date=today - timedelta(days=8),
                    account_id=self.acc.id,
                    amount=-70.0,
                    currency="NZD",
                    merchant="Suica Topup",
                    category="Transport",
                    tags=trip_tag,
                    raw_hash="tag-japan-transport",
                ),
                # Transfer should never be counted as spending
                Transaction(
                    transaction_date=today - timedelta(days=9),
                    account_id=self.acc.id,
                    amount=-500.0,
                    currency="NZD",
                    merchant="Wise Topup",
                    category="Transfers",
                    tags=trip_tag,
                    raw_hash="tag-japan-transfer",
                ),
            ]
        )
        self.db.commit()

        summary_res = self.client.get("/transactions/summary/tags?period=all")
        self.assertEqual(summary_res.status_code, 200)
        summary = summary_res.json()
        # 1200 + 130 + 70 = 1400.0 NZD
        self.assertAlmostEqual(summary["total_tagged_spend"], 1400.0, places=2)
        self.assertEqual(len(summary["tags"]), 1)
        japan = summary["tags"][0]
        self.assertEqual(japan["tag"], "japan-trip")
        self.assertAlmostEqual(japan["total_spend"], 1400.0, places=2)
        self.assertEqual(japan["transaction_count"], 3)
        by_cat = {c["category"]: c for c in japan["categories"]}
        self.assertAlmostEqual(by_cat["Travel"]["total_spend"], 1200.0, places=2)
        self.assertAlmostEqual(by_cat["Food"]["total_spend"], 130.0, places=2)
        self.assertAlmostEqual(by_cat["Transport"]["total_spend"], 70.0, places=2)
        self.assertAlmostEqual(by_cat["Travel"]["percentage"], 85.7, places=1)

        list_res = self.client.get("/transactions?tag=japan-trip&period=all&limit=2")
        self.assertEqual(list_res.status_code, 200)
        self.assertEqual(len(list_res.json()), 2)
        list_all = self.client.get("/transactions?tag=japan-trip&period=all")
        self.assertEqual(list_all.status_code, 200)
        self.assertEqual(len(list_all.json()), 4)


if __name__ == "__main__":
    unittest.main()
