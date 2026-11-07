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


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


class TestBudgetsRouter(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.client = TestClient(app)
        self.db = TestingSessionLocal()

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


if __name__ == "__main__":
    unittest.main()
