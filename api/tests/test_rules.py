"""Unit tests for Outlook-style transaction categorization rules engine and router."""

from datetime import date, timedelta
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base, get_db
from app.main import app
from app.models import Account, CategorizationRule, Category, Receipt, ReceiptItem, Transaction
from app.services.jev_client import reset_jev_circuit
from app.services.receipt_service import collect_existing_tags as receipt_collect_existing_tags
from app.services.rules_engine import collect_existing_tags, evaluate_rules, matches_rule

TEST_DB_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class TestCategorizationRules(unittest.TestCase):
    def setUp(self):
        reset_jev_circuit()
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
        reset_jev_circuit()
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

        # 3. Update rule category
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

        # 3b. Clear amount condition to null
        res_clear_null = self.client.put(
            f"/rules/{rule_id}",
            json={
                "amount_equals": None,
            },
        )
        self.assertEqual(res_clear_null.status_code, 200)
        self.assertIsNone(res_clear_null.json()["amount_equals"])

        # 3c. Clear amount condition by passing 0
        self.client.put(f"/rules/{rule_id}", json={"amount_equals": 250.0})
        res_clear_zero = self.client.put(
            f"/rules/{rule_id}",
            json={
                "amount_equals": 0,
            },
        )
        self.assertEqual(res_clear_zero.status_code, 200)
        self.assertIsNone(res_clear_zero.json()["amount_equals"])

        # 4. Delete rule
        res_del = self.client.delete(f"/rules/{rule_id}")
        self.assertEqual(res_del.status_code, 204)

        res_check = self.client.get("/rules")
        self.assertEqual(len(res_check.json()), 0)

    def test_rule_target_tags_retroactive_and_ingestion(self):
        from datetime import date
        from app.services.ingestion_service import NormalizedTx, ingest_normalized_transactions

        self.db.add(
            Transaction(
                transaction_date=date(2026, 11, 10),
                account_id=self.acc.id,
                amount=-28.81,
                currency="NZD",
                merchant="CityFitness",
                category="Health",
                raw_description="Cityfitness Group Cityfitnessg",
                raw_hash="rule-tag-hist-1",
                tags="[]",
            )
        )
        self.db.commit()

        res = self.client.post(
            "/rules",
            json={
                "name": "CityFitness Gym",
                "description_pattern": "Cityfitness",
                "match_type": "contains",
                "target_category": "Health",
                "target_tags": ["gym-membership"],
                "apply_retroactively": True,
            },
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["target_tags"], ["gym-membership"])

        txs = self.client.get("/transactions").json()
        self.assertEqual(txs[0]["tags"], ["gym-membership"])

        created, _, _ = ingest_normalized_transactions(
            self.db,
            self.acc,
            [
                NormalizedTx(
                    transaction_date=date(2026, 11, 16),
                    amount=-28.81,
                    currency="NZD",
                    merchant="CityFitness",
                    raw_description="Cityfitness Group 0Q100E",
                    category="Health",
                )
            ],
        )
        self.assertEqual(created, 1)
        txs_after = self.client.get("/transactions").json()
        self.assertEqual(txs_after[0]["tags"], ["gym-membership"])

    def test_income_rule_skips_debits_and_retroactive_skips_paired_transfers(self):
        income_rule = CategorizationRule(
            name="From Arnie",
            description_pattern="Funds Transfer",
            match_type="contains",
            amount_equals=85.0,
            target_category="Income",
            rename_merchant="From Arnie",
            is_active=True,
        )
        self.db.add(income_rule)
        self.db.flush()

        # Inflow (+85) matches Income rule; outflow (-85) must not
        self.assertTrue(matches_rule(income_rule, "Funds Transfer [DBS Savings]", 85.0))
        self.assertFalse(matches_rule(income_rule, "Funds Transfer [DBS Savings]", -85.0))

        t_in = Transaction(
            transaction_date=date(2026, 9, 30),
            account_id=self.acc.id,
            amount=85.0,
            currency="SGD",
            merchant="Funds Transfer",
            raw_description="Funds Transfer [DBS Savings]",
            raw_hash="arnie-in-1",
            category="General",
        )
        t_out = Transaction(
            transaction_date=date(2026, 10, 1),
            account_id=self.acc.id,
            amount=-85.0,
            currency="SGD",
            merchant="Funds Transfer",
            raw_description="Funds Transfer [DBS Savings]",
            raw_hash="arnie-out-1",
            category="Transfers",
        )
        t_dest = Transaction(
            transaction_date=date(2026, 10, 1),
            account_id=self.acc.id,
            amount=85.0,
            currency="SGD",
            merchant="Advice Funds Transfer",
            raw_description="Advice Funds Transfer [DBS Multiplier]",
            raw_hash="arnie-dest-1",
            category="Transfers",
        )
        self.db.add_all([t_in, t_out, t_dest])
        self.db.flush()
        t_out.linked_transaction_id = t_dest.id
        t_dest.linked_transaction_id = t_out.id
        self.db.commit()

        from app.services.rules_engine import apply_rules_to_existing_transactions

        apply_rules_to_existing_transactions(self.db, rule_ids=[income_rule.id])
        self.db.refresh(t_in)
        self.db.refresh(t_out)
        self.db.refresh(t_dest)

        self.assertEqual(t_in.category, "Income")
        self.assertEqual(t_in.merchant, "From Arnie")
        self.assertEqual(t_out.category, "Transfers")
        self.assertEqual(t_out.merchant, "Funds Transfer")
        self.assertEqual(t_dest.category, "Transfers")
        self.assertEqual(t_dest.merchant, "Advice Funds Transfer")

    def test_rule_amount_bounds_and_direction_matching(self):
        rule_fuel = CategorizationRule(
            id=1,
            name="BPme Fuel",
            description_pattern="bpme",
            match_type="contains",
            min_amount=30.0,
            max_amount=None,
            direction="outflow",
            target_category="Fuel",
            priority=5,
            is_active=True,
        )
        rule_dining = CategorizationRule(
            id=2,
            name="BPme Coffee & Snacks",
            description_pattern="bpme",
            match_type="contains",
            min_amount=None,
            max_amount=29.99,
            direction="outflow",
            target_category="Dining",
            priority=6,
            is_active=True,
        )
        rule_refund = CategorizationRule(
            id=3,
            name="BPme Fuel Refund",
            description_pattern="bpme",
            match_type="contains",
            min_amount=None,
            max_amount=None,
            direction="inflow",
            target_category="Fuel",
            rename_merchant="BP Fuel Refund",
            priority=7,
            is_active=True,
        )
        rules = [rule_fuel, rule_dining, rule_refund]

        self.assertEqual(evaluate_rules("BPME ALBANY", -12.50, rules), ("Dining", None, 2))
        self.assertEqual(evaluate_rules("BPME ALBANY", -29.99, rules), ("Dining", None, 2))
        self.assertEqual(evaluate_rules("BPME ALBANY", -30.00, rules), ("Fuel", None, 1))
        self.assertEqual(evaluate_rules("BPME ALBANY", -85.40, rules), ("Fuel", None, 1))
        self.assertEqual(
            evaluate_rules("BPME ALBANY", 45.00, rules),
            ("Fuel", "BP Fuel Refund", 3),
        )
        # Zero-amount auth rows match neither outflow (< 0) nor inflow (> 0)
        self.assertEqual(evaluate_rules("BPME ALBANY", 0.0, rules), (None, None, None))

        bounded_rule = CategorizationRule(
            name="Mid Range",
            description_pattern="coffee",
            match_type="contains",
            min_amount=10.0,
            max_amount=20.0,
            direction="outflow",
            target_category="Dining",
            is_active=True,
        )
        self.assertFalse(matches_rule(bounded_rule, "COFFEE", -9.99))
        self.assertTrue(matches_rule(bounded_rule, "COFFEE", -10.00))
        self.assertTrue(matches_rule(bounded_rule, "COFFEE", -20.00))
        self.assertFalse(matches_rule(bounded_rule, "COFFEE", -20.01))
        self.assertFalse(matches_rule(bounded_rule, "COFFEE", 15.00))

    def test_rules_api_amount_bounds_direction_crud_and_validation(self):
        today = date.today()
        tx_snack = Transaction(
            transaction_date=today,
            account_id=self.acc.id,
            amount=-14.50,
            merchant="BPME",
            raw_description="BPME ALBANY",
            category="General",
            raw_hash="bpme-snack-1",
        )
        tx_fuel = Transaction(
            transaction_date=today,
            account_id=self.acc.id,
            amount=-68.00,
            merchant="BPME",
            raw_description="BPME ALBANY",
            category="General",
            raw_hash="bpme-fuel-1",
        )
        tx_refund = Transaction(
            transaction_date=today,
            account_id=self.acc.id,
            amount=45.00,
            merchant="BPME",
            raw_description="BPME ALBANY REFUND",
            category="General",
            raw_hash="bpme-refund-1",
        )
        self.db.add_all([tx_snack, tx_fuel, tx_refund])
        self.db.commit()

        res_fuel = self.client.post(
            "/rules",
            json={
                "name": "BPme Fuel",
                "description_pattern": "BPME",
                "match_type": "contains",
                "min_amount": 30.0,
                "direction": "outflow",
                "target_category": "Fuel",
                "priority": 5,
                "apply_retroactively": True,
            },
        )
        self.assertEqual(res_fuel.status_code, 201)
        fuel_body = res_fuel.json()
        self.assertEqual(fuel_body["min_amount"], 30.0)
        self.assertIsNone(fuel_body["max_amount"])
        self.assertEqual(fuel_body["direction"], "outflow")

        self.db.expire_all()
        self.assertEqual(self.db.get(Transaction, tx_fuel.id).category, "Fuel")
        self.assertEqual(self.db.get(Transaction, tx_snack.id).category, "General")
        self.assertEqual(self.db.get(Transaction, tx_refund.id).category, "General")

        res_dining = self.client.post(
            "/rules",
            json={
                "name": "BPme Coffee",
                "description_pattern": "BPME",
                "match_type": "contains",
                "max_amount": 29.99,
                "direction": "outflow",
                "target_category": "Dining",
                "priority": 6,
                "apply_retroactively": True,
            },
        )
        self.assertEqual(res_dining.status_code, 201)
        self.db.expire_all()
        self.assertEqual(self.db.get(Transaction, tx_snack.id).category, "Dining")
        self.assertEqual(self.db.get(Transaction, tx_refund.id).category, "General")

        res_refund = self.client.post(
            "/rules",
            json={
                "name": "BPme Refund",
                "description_pattern": "BPME",
                "match_type": "contains",
                "min_amount": 10.0,
                "max_amount": 200.0,
                "direction": "inflow",
                "target_category": "Fuel",
                "rename_merchant": "BP Fuel Refund",
                "priority": 7,
                "apply_retroactively": True,
            },
        )
        self.assertEqual(res_refund.status_code, 201)
        refund_id = res_refund.json()["id"]
        self.db.expire_all()
        self.assertEqual(self.db.get(Transaction, tx_refund.id).category, "Fuel")
        self.assertEqual(self.db.get(Transaction, tx_refund.id).merchant, "BP Fuel Refund")

        # Validation errors: unsupported direction or inverted bounds
        bad_dir = self.client.post(
            "/rules",
            json={
                "name": "Bad Direction",
                "description_pattern": "TEST",
                "direction": "debit",
                "target_category": "General",
            },
        )
        self.assertEqual(bad_dir.status_code, 422)

        bad_bounds = self.client.post(
            "/rules",
            json={
                "name": "Bad Bounds",
                "description_pattern": "TEST",
                "min_amount": 50.0,
                "max_amount": 20.0,
                "target_category": "General",
            },
        )
        self.assertEqual(bad_bounds.status_code, 422)

        # Partial PUT setting min_amount above existing max_amount fails cleanly
        bad_partial = self.client.put(
            f"/rules/{refund_id}",
            json={"min_amount": 250.0},
        )
        self.assertEqual(bad_partial.status_code, 422)

        # PUT updates min_amount and clears max_amount and direction to None
        ok_clear = self.client.put(
            f"/rules/{refund_id}",
            json={"min_amount": 15.0, "max_amount": None, "direction": None},
        )
        self.assertEqual(ok_clear.status_code, 200)
        cleared = ok_clear.json()
        self.assertEqual(cleared["min_amount"], 15.0)
        self.assertIsNone(cleared["max_amount"])
        self.assertIsNone(cleared["direction"])

    def test_collect_existing_tags_reexport_and_vocab_cleanup(self):
        self.assertIs(collect_existing_tags, receipt_collect_existing_tags)

        r = Receipt(receipt_date=date(2026, 11, 1), merchant="Chemist", total_amount=20.0)
        self.db.add(r)
        self.db.flush()
        self.db.add_all(
            [
                Transaction(
                    transaction_date=date(2026, 11, 1),
                    account_id=self.acc.id,
                    amount=-15.0,
                    currency="NZD",
                    merchant="Cafe A",
                    category="Dining",
                    raw_hash="vocab-tx-1",
                    tags='["coffee", "general", "brunch"]',
                ),
                ReceiptItem(
                    receipt_id=r.id,
                    item_name="Coffee Beans",
                    unit_price=20.0,
                    total_price=20.0,
                    category="Groceries",
                    tags='["#Coffee", "general"]',
                ),
                CategorizationRule(
                    name="Coffee Rule",
                    description_pattern="Cafe",
                    target_category="Dining",
                    target_tags='["coffee", "takeaway"]',
                ),
            ]
        )
        self.db.commit()

        vocab = collect_existing_tags(self.db)
        self.assertEqual(vocab[0], "coffee")
        self.assertNotIn("general", vocab)
        self.assertEqual(set(vocab), {"coffee", "brunch", "takeaway"})

    def test_classify_uncategorized_dry_run_exclusions_cache_and_commit(self):
        d = date(2026, 11, 10)
        self.db.add(Category(name="Pets", is_active=True, is_system=False, sort_order=150))
        self.db.add_all(
            [
                CategorizationRule(
                    name="Active Rule",
                    description_pattern="ACTIVE GROCERY TARGET",
                    match_type="contains",
                    target_category="Groceries",
                    priority=1,
                    is_active=True,
                ),
                CategorizationRule(
                    name="Inactive Rule",
                    description_pattern="DORMANT RULE TARGET",
                    match_type="contains",
                    target_category="Groceries",
                    priority=2,
                    is_active=False,
                ),
            ]
        )

        tx_paired_1 = Transaction(
            transaction_date=d,
            account_id=self.acc.id,
            amount=-100.0,
            currency="NZD",
            merchant="Linked Out",
            category="General",
            raw_hash="cu-paired-1",
            tags="[]",
        )
        tx_paired_2 = Transaction(
            transaction_date=d,
            account_id=self.acc.id,
            amount=100.0,
            currency="NZD",
            merchant="Linked In",
            category="General",
            raw_hash="cu-paired-2",
            tags="[]",
        )
        self.db.add_all([tx_paired_1, tx_paired_2])
        self.db.flush()
        tx_paired_1.linked_transaction_id = tx_paired_2.id
        tx_paired_2.linked_transaction_id = tx_paired_1.id

        tx_transfer = Transaction(
            transaction_date=d,
            account_id=self.acc.id,
            amount=-50.0,
            currency="NZD",
            merchant="Internal Tfr",
            category="Transfers",
            raw_hash="cu-tfr",
            tags="[]",
        )
        tx_income_existing = Transaction(
            transaction_date=d,
            account_id=self.acc.id,
            amount=500.0,
            currency="NZD",
            merchant="Payroll",
            category="Income",
            raw_hash="cu-inc-existing",
            tags="[]",
        )
        tx_rule = Transaction(
            transaction_date=d,
            account_id=self.acc.id,
            amount=-40.0,
            currency="NZD",
            merchant="ACTIVE GROCERY TARGET",
            raw_description="ACTIVE GROCERY TARGET STORE",
            category="General",
            raw_hash="cu-rule",
            tags="[]",
        )
        tx_inactive = Transaction(
            transaction_date=d,
            account_id=self.acc.id,
            amount=-42.0,
            currency="NZD",
            merchant="DORMANT RULE TARGET",
            category="General",
            raw_hash="cu-inactive-rule",
            tags="[]",
        )
        tx_outflow_income_reject = Transaction(
            transaction_date=d,
            account_id=self.acc.id,
            amount=-30.0,
            currency="NZD",
            merchant="Weird Refund",
            category="General",
            raw_hash="cu-out-inc",
            tags="[]",
        )
        tx_inflow_valid = Transaction(
            transaction_date=d,
            account_id=self.acc.id,
            amount=1500.0,
            currency="NZD",
            merchant="Consulting Payout",
            category="General",
            raw_hash="cu-in-inc",
            tags="[]",
        )
        tx_tagged_general = Transaction(
            transaction_date=d,
            account_id=self.acc.id,
            amount=-25.0,
            currency="NZD",
            merchant="Animates",
            category="General",
            raw_hash="cu-pets",
            tags='["dog", "general"]',
        )
        tx_cache_1 = Transaction(
            transaction_date=d,
            account_id=self.acc.id,
            amount=-5.50,
            currency="NZD",
            merchant="Mojo Coffee",
            category="General",
            raw_hash="cu-cache-1",
            notes="akahu:tx_1",
            tags="[]",
        )
        tx_cache_2 = Transaction(
            transaction_date=d - timedelta(days=1),
            account_id=self.acc.id,
            amount=-5.50,
            currency="NZD",
            merchant="  mojo coffee ",
            category="General",
            raw_hash="cu-cache-2",
            notes="akahu:tx_2",
            tags="[]",
        )
        tx_untagged_non_gen = Transaction(
            transaction_date=d,
            account_id=self.acc.id,
            amount=-65.0,
            currency="NZD",
            merchant="Mecca",
            category="Personal Care",
            raw_hash="cu-untagged-pc",
            tags="[]",
        )
        tx_already_tagged = Transaction(
            transaction_date=d,
            account_id=self.acc.id,
            amount=-18.0,
            currency="NZD",
            merchant="Sushi Bar",
            category="Dining",
            raw_hash="cu-already-tagged",
            tags='["lunch"]',
        )
        self.db.add_all(
            [
                tx_transfer,
                tx_income_existing,
                tx_rule,
                tx_inactive,
                tx_outflow_income_reject,
                tx_inflow_valid,
                tx_tagged_general,
                tx_cache_1,
                tx_cache_2,
                tx_untagged_non_gen,
                tx_already_tagged,
            ]
        )
        self.db.commit()

        jev_calls = []

        def fake_jev(merchant, amount, notes=None, existing_tags=None, extra_categories=None):
            jev_calls.append(
                {
                    "merchant": merchant,
                    "amount": amount,
                    "notes": notes,
                    "existing_tags": existing_tags,
                    "extra_categories": extra_categories,
                }
            )
            m = merchant.strip().lower()
            if "dormant" in m:
                return ("Shopping", False, ["general", "shopping", "retail"])
            if "weird" in m:
                return ("Income", False, ["refund"])
            if "consulting" in m:
                return ("Income", False, ["salary", "income"])
            if "animates" in m:
                return ("Pets", False, ["pets", "general", "treats"])
            if "mojo" in m:
                return ("Dining", False, ["coffee", "dining"])
            if "mecca" in m:
                return ("Shopping", False, ["personal-care", "general", "skincare"])
            return ("General", False, [])

        with patch("app.services.rules_engine.classify_transaction_jev_with_tags", side_effect=fake_jev):
            # 1. Default empty POST body -> dry_run=True, include_untagged=False
            dry_res = self.client.post("/rules/classify-uncategorized")
            self.assertEqual(dry_res.status_code, 200)
            dry_body = dry_res.json()
            self.assertTrue(dry_body["dry_run"])
            self.assertEqual(dry_body["evaluated"], 6)
            self.assertEqual(dry_body["updated"], 6)
            self.assertEqual(dry_body["skipped_circuit_open"], 0)
            self.assertEqual(dry_body["remaining"], 0)
            weird_change = next(
                c for c in dry_body["changes"] if c["transaction_id"] == tx_outflow_income_reject.id
            )
            self.assertEqual(weird_change["old_category"], "General")
            self.assertEqual(weird_change["new_category"], "General")
            self.assertEqual(weird_change["new_tags"], ["refund"])
            # Mojo Coffee evaluated twice via 1 Jev call (in-batch cache)
            mojo_calls = [c for c in jev_calls if "mojo" in c["merchant"].lower()]
            self.assertEqual(len(mojo_calls), 1)
            self.assertEqual(mojo_calls[0]["notes"], "akahu:tx_1")
            self.assertEqual(mojo_calls[0]["extra_categories"], ["Pets"])
            self.assertIn("dog", mojo_calls[0]["existing_tags"])
            self.assertNotIn("general", mojo_calls[0]["existing_tags"])

            # Verify DB unchanged after dry_run=True
            self.db.expire_all()
            self.assertEqual(self.db.get(Transaction, tx_tagged_general.id).category, "General")
            self.assertEqual(self.db.get(Transaction, tx_cache_1.id).category, "General")

            # 2. only_outflows=True skips tx_inflow_valid
            jev_calls.clear()
            outflow_res = self.client.post(
                "/rules/classify-uncategorized",
                json={"only_outflows": True, "dry_run": True},
            )
            self.assertEqual(outflow_res.status_code, 200)
            self.assertEqual(outflow_res.json()["evaluated"], 5)
            self.assertEqual(outflow_res.json()["updated"], 5)

            # 3. include_untagged=True & dry_run=False preserves Personal Care while adding skincare
            jev_calls.clear()
            apply_res = self.client.post(
                "/rules/classify-uncategorized",
                json={"include_untagged": True, "dry_run": False},
            )
            self.assertEqual(apply_res.status_code, 200)
            apply_body = apply_res.json()
            self.assertFalse(apply_body["dry_run"])
            self.assertEqual(apply_body["evaluated"], 7)
            self.assertEqual(apply_body["updated"], 7)

        self.db.expire_all()
        self.assertEqual(self.db.get(Transaction, tx_paired_1.id).category, "General")
        self.assertEqual(self.db.get(Transaction, tx_transfer.id).category, "Transfers")
        self.assertEqual(self.db.get(Transaction, tx_income_existing.id).category, "Income")
        self.assertEqual(self.db.get(Transaction, tx_rule.id).category, "General")
        self.assertEqual(self.db.get(Transaction, tx_inactive.id).category, "Shopping")
        self.assertEqual(json.loads(self.db.get(Transaction, tx_inactive.id).tags), ["retail"])
        # Outflow 'Income' verdict rejected -> stays General
        self.assertEqual(self.db.get(Transaction, tx_outflow_income_reject.id).category, "General")
        self.assertEqual(json.loads(self.db.get(Transaction, tx_outflow_income_reject.id).tags), ["refund"])
        # Inflow 'Income' verdict accepted
        self.assertEqual(self.db.get(Transaction, tx_inflow_valid.id).category, "Income")
        self.assertEqual(json.loads(self.db.get(Transaction, tx_inflow_valid.id).tags), ["salary"])
        # Existing tags merged and 'general' + category tag stripped
        self.assertEqual(self.db.get(Transaction, tx_tagged_general.id).category, "Pets")
        self.assertEqual(
            json.loads(self.db.get(Transaction, tx_tagged_general.id).tags),
            ["dog", "treats"],
        )
        # Cached duplicates both updated
        self.assertEqual(self.db.get(Transaction, tx_cache_1.id).category, "Dining")
        self.assertEqual(self.db.get(Transaction, tx_cache_2.id).category, "Dining")
        self.assertEqual(json.loads(self.db.get(Transaction, tx_cache_2.id).tags), ["coffee"])
        # Non-General untagged preserves Personal Care and strips 'personal-care'
        pc_tx = self.db.get(Transaction, tx_untagged_non_gen.id)
        self.assertEqual(pc_tx.category, "Personal Care")
        self.assertEqual(json.loads(pc_tx.tags), ["skincare"])
        # Already-tagged Dining untouched
        self.assertEqual(json.loads(self.db.get(Transaction, tx_already_tagged.id).tags), ["lunch"])

    def test_classify_uncategorized_limit_remaining_and_circuit_breaker(self):
        txs = [
            Transaction(
                transaction_date=date(2026, 11, 1) + timedelta(days=i),
                account_id=self.acc.id,
                amount=-(10.0 + i),
                currency="NZD",
                merchant=f"Merchant {i}",
                category="General",
                raw_hash=f"cb-tx-{i}",
                tags="[]",
            )
            for i in range(4)
        ]
        self.db.add_all(txs)
        self.db.commit()

        with (
            patch(
                "app.services.rules_engine.is_jev_circuit_open",
                side_effect=[False, True],
            ),
            patch(
                "app.services.rules_engine.classify_transaction_jev_with_tags",
                return_value=("Dining", False, ["coffee"]),
            ) as mock_jev,
        ):
            res = self.client.post(
                "/rules/classify-uncategorized",
                json={"limit": 3, "dry_run": False},
            )

        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(mock_jev.call_count, 1)
        self.assertEqual(body["evaluated"], 1)
        self.assertEqual(body["updated"], 1)
        self.assertEqual(body["skipped_circuit_open"], 2)
        self.assertEqual(body["remaining"], 1)
        # Newest row (i=3) evaluated first and persisted; older rows untouched
        self.assertEqual(body["changes"][0]["transaction_id"], txs[3].id)

        self.db.expire_all()
        self.assertEqual(self.db.get(Transaction, txs[3].id).category, "Dining")
        self.assertEqual(self.db.get(Transaction, txs[2].id).category, "General")
        self.assertEqual(self.db.get(Transaction, txs[1].id).category, "General")
        self.assertEqual(self.db.get(Transaction, txs[0].id).category, "General")


if __name__ == "__main__":
    unittest.main()
