"""Unit tests for budgets and category spending breakdown router."""

from datetime import date, timedelta
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
from app.models import Account, BudgetRule, Transaction

# In-memory SQLite for testing with StaticPool so all connections share the memory DB
TEST_DB_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class TestBudgetsRouter(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()

        def override_get_db():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

        # Seed an account
        self.acc = Account(
            name="Primary Checking",
            account_type="checking",
            tier="cash",
            institution="ANZ",
            currency="NZD",
        )
        self.db.add(self.acc)
        self.db.commit()
        self.db.refresh(self.acc)

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_upsert_budget_rule(self):
        # Create
        res = self.client.post("/budgets", json={"category": "Groceries", "weekly_limit": 200.0})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["category"], "Groceries")
        self.assertEqual(data["weekly_limit"], 200.0)
        self.assertAlmostEqual(data["monthly_limit"], 866.67, places=1)

        # Update
        res_up = self.client.post("/budgets", json={"category": "Groceries", "weekly_limit": 250.0})
        self.assertEqual(res_up.status_code, 200)
        self.assertEqual(res_up.json()["weekly_limit"], 250.0)

    def test_init_defaults(self):
        res = self.client.post("/budgets/init-defaults")
        self.assertEqual(res.status_code, 200)
        rules = res.json()
        self.assertGreaterEqual(len(rules), 6)
        categories = [r["category"] for r in rules]
        self.assertIn("Groceries", categories)
        self.assertIn("Transport", categories)

    def test_spending_breakdown_and_warnings(self):
        # Create budget for Groceries: $100/mo
        self.client.post("/budgets", json={"category": "Groceries", "monthly_limit": 100.0})

        today = date.today()
        # Add transactions in current month
        # 1. Groceries: $150 (over budget!)
        tx1 = Transaction(
            transaction_date=today,
            account_id=self.acc.id,
            amount=-150.0,
            merchant="Pak n Save",
            category="Groceries",
            raw_hash="hash_groceries_1",
        )
        # 2. Food: $50 (no budget set)
        tx2 = Transaction(
            transaction_date=today,
            account_id=self.acc.id,
            amount=-50.0,
            merchant="Cafe",
            category="Food",
            raw_hash="hash_food_1",
        )
        # 3. Income: $1000
        tx3 = Transaction(
            transaction_date=today,
            account_id=self.acc.id,
            amount=1000.0,
            merchant="Salary",
            category="Income",
            raw_hash="hash_income_1",
        )
        self.db.add_all([tx1, tx2, tx3])
        self.db.commit()

        # Query breakdown
        res = self.client.get("/budgets/breakdown?period=month")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["total_spend"], 200.0)
        self.assertEqual(body["total_income"], 1000.0)
        self.assertEqual(body["net_savings"], 800.0)
        self.assertEqual(body["over_budget_count"], 1)

        groceries_report = next(c for c in body["categories"] if c["category"] == "Groceries")
        self.assertEqual(groceries_report["total_spend"], 150.0)
        self.assertEqual(groceries_report["percentage"], 75.0)
        self.assertEqual(groceries_report["budget_limit"], 100.0)
        self.assertEqual(groceries_report["percent_used"], 150.0)
        self.assertTrue(groceries_report["is_over_budget"])
        self.assertEqual(groceries_report["warning_level"], "danger")

        food_report = next(c for c in body["categories"] if c["category"] == "Food")
        self.assertEqual(food_report["total_spend"], 50.0)
        self.assertEqual(food_report["percentage"], 25.0)
        self.assertFalse(food_report["is_over_budget"])

    def test_transfer_exclusion_from_spending_breakdown(self):
        today = date.today()
        # Add normal expense
        tx1 = Transaction(
            transaction_date=today,
            account_id=self.acc.id,
            amount=-80.0,
            merchant="Countdown",
            category="Groceries",
            raw_hash="hash_groceries_transfer_test",
        )
        # Add transfer to Wise (-$500)
        tx_transfer = Transaction(
            transaction_date=today,
            account_id=self.acc.id,
            amount=-500.0,
            merchant="Transfer to Wise",
            category="Transfers",
            raw_hash="hash_wise_transfer_1",
        )
        self.db.add_all([tx1, tx_transfer])
        self.db.commit()

        res = self.client.get("/budgets/breakdown?period=month")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        # total_spend must NOT include the $500 transfer!
        self.assertEqual(body["total_spend"], 80.0)
        self.assertEqual(body["total_transfers"], 500.0)

        transfer_cat = next((c for c in body["categories"] if c["category"] == "Transfers"), None)
        self.assertIsNotNone(transfer_cat)
        self.assertTrue(transfer_cat["is_transfer"])
        self.assertEqual(transfer_cat["total_spend"], 500.0)
        self.assertEqual(transfer_cat["percentage"], 0.0)
        self.assertFalse(transfer_cat["is_over_budget"])

    def test_patch_transaction_category_and_list_filter(self):
        today = date.today()
        tx = Transaction(
            transaction_date=today,
            account_id=self.acc.id,
            amount=-45.0,
            merchant="Unknown Vendor",
            category="General",
            raw_hash="hash_unknown_vendor_1",
        )
        self.db.add(tx)
        self.db.commit()
        self.db.refresh(tx)

        # 1. Patch category to "Food"
        patch_res = self.client.patch(f"/transactions/{tx.id}", json={"category": "Food"})
        self.assertEqual(patch_res.status_code, 200)
        patched_data = patch_res.json()
        self.assertEqual(patched_data["category"], "Food")
        self.assertEqual(patched_data["account_name"], "Primary Checking")

        # 2. Filter list by category and period
        list_res = self.client.get("/transactions?category=food&period=month")
        self.assertEqual(list_res.status_code, 200)
        tx_list = list_res.json()
        self.assertGreaterEqual(len(tx_list), 1)
        self.assertEqual(tx_list[0]["id"], tx.id)
        self.assertEqual(tx_list[0]["category"], "Food")

    def test_auto_categorize_transfers(self):
        from app.services.statement_parser import auto_categorize
        self.assertEqual(auto_categorize("TRANSFER TO WISE"), "Transfers")
        self.assertEqual(auto_categorize("TFR TO ASB FREEDOM"), "Transfers")
        self.assertEqual(auto_categorize("INTERNAL TRANSFER 12-3456"), "Transfers")
        self.assertEqual(auto_categorize("TRANSFERWISE NZD"), "Transfers")
        self.assertEqual(auto_categorize("SHARESIES WALLET"), "Transfers")

    def test_last_month_and_account_id_filter(self):
        today = date.today()
        first_this_month = date(today.year, today.month, 1)
        last_day_prev_month = first_this_month - timedelta(days=1)
        prev_month_mid = date(last_day_prev_month.year, last_day_prev_month.month, 15)

        # Transaction in last month
        tx_prev = Transaction(
            transaction_date=prev_month_mid,
            account_id=self.acc.id,
            amount=-80.0,
            merchant="Countdown",
            category="Groceries",
            raw_hash="hash_prev_month_1",
        )
        # Transaction in this month
        tx_curr = Transaction(
            transaction_date=today,
            account_id=self.acc.id,
            amount=-50.0,
            merchant="Pak n Save",
            category="Groceries",
            raw_hash="hash_curr_month_1",
        )
        self.db.add_all([tx_prev, tx_curr])
        self.db.commit()

        # Query transactions for last_month
        res = self.client.get(f"/transactions?period=last_month&account_id={self.acc.id}")
        self.assertEqual(res.status_code, 200)
        items = res.json()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["merchant"], "Countdown")

        # Query spending breakdown for last_month
        b_res = self.client.get("/budgets/breakdown?period=last_month")
        self.assertEqual(b_res.status_code, 200)
        b_data = b_res.json()
        self.assertEqual(b_data["period"], "last_month")
        self.assertEqual(b_data["total_spend"], 80.0)

    def test_week_and_last_week_breakdown(self):
        res_week = self.client.get("/budgets/breakdown?period=week")
        self.assertEqual(res_week.status_code, 200)
        self.assertIn("total_spend", res_week.json())

        res_last_week = self.client.get("/budgets/breakdown?period=last_week")
        self.assertEqual(res_last_week.status_code, 200)
        self.assertIn("total_spend", res_last_week.json())

    def test_monthly_amortized_reconciliation_off_month(self):
        today = date.today()
        first_this_month = date(today.year, today.month, 1)
        sgd_acc = Account(
            name="DBS Multiplier",
            account_type="savings",
            tier="cash",
            institution="DBS Bank",
            currency="SGD",
        )
        self.db.add(sgd_acc)
        self.db.commit()
        self.db.refresh(sgd_acc)

        self.db.add_all(
            [
                Transaction(
                    transaction_date=today,
                    account_id=self.acc.id,
                    amount=-1.99,
                    currency="NZD",
                    merchant="iCloud",
                    category="Utilities",
                    recurrence="monthly",
                    raw_hash="util-icloud-curr",
                ),
                Transaction(
                    transaction_date=first_this_month,
                    account_id=self.acc.id,
                    amount=-8.00,
                    currency="NZD",
                    merchant="SKINNY MOBIL",
                    category="Utilities",
                    recurrence="monthly",
                    raw_hash="util-skinny-curr",
                ),
                Transaction(
                    transaction_date=first_this_month - timedelta(days=45),
                    account_id=self.acc.id,
                    amount=-279.99,
                    currency="NZD",
                    merchant="Google One",
                    category="Utilities",
                    recurrence="annual",
                    raw_hash="util-google-one-annual",
                ),
                Transaction(
                    transaction_date=first_this_month - timedelta(days=35),
                    account_id=sgd_acc.id,
                    amount=-120.00,
                    currency="SGD",
                    exchange_rate=1.3694,
                    merchant="ANNUAL FEE",
                    category="Utilities",
                    recurrence="annual",
                    raw_hash="util-annual-fee-sgd",
                ),
            ]
        )
        self.db.commit()

        b_res = self.client.get("/budgets/breakdown?period=month")
        self.assertEqual(b_res.status_code, 200)
        utilities = next(c for c in b_res.json()["categories"] if c["category"] == "Utilities")
        self.assertAlmostEqual(utilities["total_spend"], 46.99, places=2)
        self.assertEqual(utilities["transaction_count"], 4)
        self.assertEqual(utilities["actual_transaction_count"], 2)
        self.assertEqual(utilities["amortized_count"], 2)

        # Default transactions endpoint stays strictly dated within current month
        default_txs = self.client.get("/transactions?category=Utilities&period=month")
        self.assertEqual(default_txs.status_code, 200)
        self.assertEqual(len(default_txs.json()), 2)

        # Reconciled inspection includes both dated transactions and amortized annual commitments
        reconciled_res = self.client.get(
            "/transactions?category=Utilities&period=month&include_amortized=true"
        )
        self.assertEqual(reconciled_res.status_code, 200)
        items = reconciled_res.json()
        self.assertEqual(len(items), 4)

        actual = [tx for tx in items if not tx["is_amortized_commitment"]]
        amortized = [tx for tx in items if tx["is_amortized_commitment"]]
        self.assertEqual(len(actual), 2)
        self.assertEqual(len(amortized), 2)
        self.assertTrue(all(tx["amortized_cadence"] == "month" for tx in amortized))
        self.assertTrue(all(not tx["excluded_from_period_budget"] for tx in items))

        reconciled_total = sum(abs(tx["amount"]) for tx in actual) + sum(
            tx["amortized_period_amount_nzd"] for tx in amortized
        )
        self.assertAlmostEqual(reconciled_total, utilities["total_spend"], places=2)

    def test_monthly_amortized_deduplicates_in_month_annual_charge(self):
        today = date.today()
        first_this_month = date(today.year, today.month, 1)

        self.db.add_all(
            [
                Transaction(
                    transaction_date=today,
                    account_id=self.acc.id,
                    amount=-81.25,
                    currency="NZD",
                    merchant="Southern Cross",
                    category="Insurance",
                    recurrence="fortnightly",
                    raw_hash="ins-sc-1",
                ),
                Transaction(
                    transaction_date=first_this_month,
                    account_id=self.acc.id,
                    amount=-81.25,
                    currency="NZD",
                    merchant="Southern Cross",
                    category="Insurance",
                    recurrence="fortnightly",
                    raw_hash="ins-sc-2",
                ),
                Transaction(
                    transaction_date=first_this_month,
                    account_id=self.acc.id,
                    amount=-1786.72,
                    currency="NZD",
                    merchant="Aioi Nissay Dowa Insurance",
                    category="Insurance",
                    recurrence="annual",
                    raw_hash="ins-aioi-annual",
                ),
            ]
        )
        self.db.commit()

        b_res = self.client.get("/budgets/breakdown?period=month")
        self.assertEqual(b_res.status_code, 200)
        insurance = next(c for c in b_res.json()["categories"] if c["category"] == "Insurance")
        self.assertAlmostEqual(insurance["total_spend"], 311.39, places=2)
        self.assertEqual(insurance["transaction_count"], 3)
        self.assertEqual(insurance["actual_transaction_count"], 2)
        self.assertEqual(insurance["amortized_count"], 1)

        tx_res = self.client.get(
            "/transactions?category=Insurance&period=month&include_amortized=true"
        )
        self.assertEqual(tx_res.status_code, 200)
        items = tx_res.json()
        self.assertEqual(len(items), 3)
        self.assertEqual(len({tx["id"] for tx in items}), 3)

        aioi_rows = [tx for tx in items if tx["merchant"] == "Aioi Nissay Dowa Insurance"]
        self.assertEqual(len(aioi_rows), 1)
        self.assertTrue(aioi_rows[0]["is_amortized_commitment"])
        self.assertAlmostEqual(aioi_rows[0]["amortized_period_amount_nzd"], 148.89, places=2)
        self.assertEqual(aioi_rows[0]["amortized_cadence"], "month")

    def test_weekly_amortized_reconciliation(self):
        today = date.today()
        self.db.add_all(
            [
                Transaction(
                    transaction_date=today,
                    account_id=self.acc.id,
                    amount=-25.00,
                    currency="NZD",
                    merchant="Cafe Lunch",
                    category="Food",
                    recurrence=None,
                    raw_hash="wk-food-var",
                ),
                Transaction(
                    transaction_date=today - timedelta(days=10),
                    account_id=self.acc.id,
                    amount=-104.00,
                    currency="NZD",
                    merchant="Meal Prep Box",
                    category="Food",
                    recurrence="monthly",
                    raw_hash="wk-food-monthly",
                ),
            ]
        )
        self.db.commit()

        b_res = self.client.get("/budgets/breakdown?period=week")
        self.assertEqual(b_res.status_code, 200)
        food = next(c for c in b_res.json()["categories"] if c["category"] == "Food")
        self.assertAlmostEqual(food["total_spend"], 49.00, places=2)
        self.assertEqual(food["transaction_count"], 2)
        self.assertEqual(food["actual_transaction_count"], 1)
        self.assertEqual(food["amortized_count"], 1)

        tx_res = self.client.get("/transactions?category=Food&period=week&include_amortized=true")
        self.assertEqual(tx_res.status_code, 200)
        items = tx_res.json()
        self.assertEqual(len(items), 2)
        amortized = [tx for tx in items if tx["is_amortized_commitment"]]
        self.assertEqual(len(amortized), 1)
        self.assertEqual(amortized[0]["amortized_cadence"], "week")
        self.assertAlmostEqual(amortized[0]["amortized_period_amount_nzd"], 24.00, places=2)

    def test_excluded_one_off_and_positive_refund_flagged(self):
        today = date.today()
        self.db.add_all(
            [
                Transaction(
                    transaction_date=today,
                    account_id=self.acc.id,
                    amount=-50.00,
                    currency="NZD",
                    merchant="USB Cable",
                    category="Tech",
                    recurrence=None,
                    raw_hash="tech-var",
                ),
                Transaction(
                    transaction_date=today,
                    account_id=self.acc.id,
                    amount=-1200.00,
                    currency="NZD",
                    merchant="Workstation Monitor",
                    category="Tech",
                    recurrence="one-off",
                    raw_hash="tech-oneoff",
                ),
                Transaction(
                    transaction_date=today,
                    account_id=self.acc.id,
                    amount=15.00,
                    currency="NZD",
                    merchant="Cable Refund",
                    category="Tech",
                    recurrence=None,
                    raw_hash="tech-refund",
                ),
            ]
        )
        self.db.commit()

        b_res = self.client.get("/budgets/breakdown?period=month")
        self.assertEqual(b_res.status_code, 200)
        tech = next(c for c in b_res.json()["categories"] if c["category"] == "Tech")
        self.assertAlmostEqual(tech["total_spend"], 50.00, places=2)
        self.assertEqual(tech["transaction_count"], 1)
        self.assertEqual(tech["actual_transaction_count"], 1)
        self.assertEqual(tech["amortized_count"], 0)

        tx_res = self.client.get("/transactions?category=Tech&period=month&include_amortized=true")
        self.assertEqual(tx_res.status_code, 200)
        by_merchant = {tx["merchant"]: tx for tx in tx_res.json()}
        self.assertEqual(len(by_merchant), 3)
        self.assertFalse(by_merchant["USB Cable"]["excluded_from_period_budget"])
        self.assertTrue(by_merchant["Workstation Monitor"]["excluded_from_period_budget"])
        self.assertTrue(by_merchant["Cable Refund"]["excluded_from_period_budget"])

    def test_base_class_amortized_rollup_and_custom_range(self):
        today = date.today()
        first_this_month = date(today.year, today.month, 1)
        self.db.add_all(
            [
                Transaction(
                    transaction_date=today,
                    account_id=self.acc.id,
                    amount=-20.00,
                    currency="NZD",
                    merchant="AT Hop",
                    category="Transport",
                    recurrence=None,
                    raw_hash="tr-hop",
                ),
                Transaction(
                    transaction_date=first_this_month - timedelta(days=40),
                    account_id=self.acc.id,
                    amount=-520.00,
                    currency="NZD",
                    merchant="AA Registration",
                    category="Vehicle",
                    recurrence="annual",
                    raw_hash="tr-veh-annual",
                ),
            ]
        )
        self.db.commit()

        b_res = self.client.get("/budgets/breakdown?period=month&group_by=base")
        self.assertEqual(b_res.status_code, 200)
        transport = next(c for c in b_res.json()["categories"] if c["category"] == "Transport")
        self.assertAlmostEqual(transport["total_spend"], 63.33, places=2)
        self.assertEqual(transport["transaction_count"], 2)
        self.assertEqual(transport["actual_transaction_count"], 1)
        self.assertEqual(transport["amortized_count"], 1)

        tx_res = self.client.get(
            "/transactions?category=Transport&period=month&include_subclasses=true&include_amortized=true"
        )
        self.assertEqual(tx_res.status_code, 200)
        items = tx_res.json()
        self.assertEqual(len(items), 2)
        amortized = [tx for tx in items if tx["is_amortized_commitment"]]
        self.assertEqual(len(amortized), 1)
        self.assertEqual(amortized[0]["merchant"], "AA Registration")
        self.assertAlmostEqual(amortized[0]["amortized_period_amount_nzd"], 43.33, places=2)

        custom_res = self.client.get(
            f"/budgets/breakdown?start_date={first_this_month.isoformat()}&end_date={today.isoformat()}&group_by=base"
        )
        self.assertEqual(custom_res.status_code, 200)
        custom_transport = next(
            c for c in custom_res.json()["categories"] if c["category"] == "Transport"
        )
        self.assertAlmostEqual(custom_transport["total_spend"], 20.00, places=2)
        self.assertEqual(custom_transport["transaction_count"], 1)
        self.assertEqual(custom_transport["amortized_count"], 0)


if __name__ == "__main__":
    unittest.main()
