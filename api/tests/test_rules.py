"""Unit tests for Outlook-style transaction categorization rules engine and router."""

from datetime import date
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
from app.models import Account, CategorizationRule, Transaction
from app.services.rules_engine import evaluate_rules, matches_rule

TEST_DB_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class TestCategorizationRules(unittest.TestCase):
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

        self.acc = Account(
            name="Checking Account",
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

    def test_matches_rule_logic(self):
        # 1. Rule with description contains and amount condition
        rule1 = CategorizationRule(
            name="Utilities Regular",
            description_pattern="12-0323-0895056",
            match_type="contains",
            amount_equals=400.0,
            target_category="Utilities",
            rename_merchant="Power/Water Utilities",
            is_active=True,
        )
        # Matches matching desc and 400
        self.assertTrue(matches_rule(rule1, "TRANSFER TO 12-0323-0895056-00", -400.0))
        self.assertTrue(matches_rule(rule1, "TFR 12-0323-0895056 REF UTILITY", 400.0))
        # Does NOT match different amount
        self.assertFalse(matches_rule(rule1, "TRANSFER TO 12-0323-0895056-00", -350.0))
        # Does NOT match different desc
        self.assertFalse(matches_rule(rule1, "TRANSFER TO OTHER ACCOUNT", -400.0))

        # 2. Rule with any amount
        rule2 = CategorizationRule(
            name="Pak n Save Groceries",
            description_pattern="pak n save",
            match_type="contains",
            amount_equals=None,
            target_category="Groceries",
            is_active=True,
        )
        self.assertTrue(matches_rule(rule2, "PAK N SAVE ALBANY", -123.45))
        self.assertTrue(matches_rule(rule2, "PAK N SAVE SILVERDALE", -12.00))

    def test_rules_api_crud_and_retroactive_apply(self):
        today = date.today()
        # Seed an uncategorized transaction
        tx = Transaction(
            transaction_date=today,
            account_id=self.acc.id,
            amount=-400.0,
            merchant="Transfer to 12-3456",
            raw_description="TRANSFER TO 12-3456-7890 REF UTILITIES",
            category="General",
            raw_hash="hash_test_tx_400",
        )
        self.db.add(tx)
        self.db.commit()

        # 1. Create rule with retroactive apply
        res = self.client.post(
            "/rules",
            json={
                "name": "Utilities Monthly Transfer",
                "description_pattern": "12-3456-7890",
                "match_type": "contains",
                "amount_equals": 400.0,
                "target_category": "Utilities",
                "rename_merchant": "Power & Water Utilities",
                "apply_retroactively": True,
            },
        )
        self.assertEqual(res.status_code, 201)
        created = res.json()
        rule_id = created["id"]
        self.assertEqual(created["name"], "Utilities Monthly Transfer")
        self.assertEqual(created["target_category"], "Utilities")

        # Verify transaction was retroactively updated
        self.db.refresh(tx)
        self.assertEqual(tx.category, "Utilities")
        self.assertEqual(tx.merchant, "Power & Water Utilities")

        # 2. List rules
        res_list = self.client.get("/rules")
        self.assertEqual(res_list.status_code, 200)
        self.assertEqual(len(res_list.json()), 1)
        self.assertEqual(res_list.json()[0]["match_count"], 1)

        # 3. Update rule
        res_up = self.client.put(
            f"/rules/{rule_id}",
            json={
                "target_category": "Housing",
                "apply_retroactively": True,
            },
        )
        self.assertEqual(res_up.status_code, 200)
        self.assertEqual(res_up.json()["target_category"], "Housing")

        self.db.refresh(tx)
        self.assertEqual(tx.category, "Housing")

        # 4. Delete rule
        res_del = self.client.delete(f"/rules/{rule_id}")
        self.assertEqual(res_del.status_code, 204)

        res_check = self.client.get("/rules")
        self.assertEqual(len(res_check.json()), 0)


if __name__ == "__main__":
    unittest.main()
