"""Unit tests for dynamic hierarchical categories, 52w amortization, base rollups, and regex fixes."""

from datetime import date, timedelta
from pathlib import Path
import sys
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base, get_db
from app.main import app
from app.models import (
    Account,
    AssetSnapshot,
    BudgetRule,
    CategorizationRule,
    Category,
    Product,
    Receipt,
    ReceiptItem,
    Transaction,
    TransactionSplit,
)
from app.services.category_taxonomy import normalize_existing_legacy_categories
from app.services.receipt_ingestion import classify_line_item
from app.services.statement_parser import auto_categorize

TEST_DB_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class TestCategoriesRouterAndHierarchy(unittest.TestCase):
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

    def test_get_categories_auto_seeds_approved_hierarchy_with_counts_and_budgets(self):
        today = date.today()
        self.db.add(
            Transaction(
                transaction_date=today,
                account_id=self.acc.id,
                amount=-24.00,
                currency="NZD",
                merchant="Super Liquor",
                category="Alcohol",
                raw_hash="cat-seed-alc",
            )
        )
        self.db.add(
            CategorizationRule(
                name="Alcohol Rule",
                description_pattern="Super Liquor",
                match_type="contains",
                target_category="Alcohol",
            )
        )
        self.db.add(
            BudgetRule(
                category="Groceries",
                weekly_limit=200.0,
                monthly_limit=866.67,
                budget_period="monthly",
                is_active=True,
            )
        )
        self.db.add(
            BudgetRule(
                category="Travel",
                weekly_limit=100.0,
                monthly_limit=433.33,
                budget_period="annual",
                is_active=True,
            )
        )
        self.db.commit()

        res = self.client.get("/categories")
        self.assertEqual(res.status_code, 200)
        cats = {c["name"]: c for c in res.json()}

        self.assertEqual(cats["Dining"]["parent_name"], "Food")
        self.assertEqual(cats["Groceries"]["parent_name"], "Food")
        self.assertEqual(cats["Alcohol"]["parent_name"], "Food")
        self.assertEqual(cats["Food"]["child_count"], 3)
        self.assertEqual(cats["Fees & Admin"]["parent_name"], "Finance")
        self.assertEqual(cats["Debt & Loans"]["parent_name"], "Finance")
        self.assertEqual(cats["Finance"]["child_count"], 2)

        self.assertEqual(cats["Alcohol"]["transaction_count"], 1)
        self.assertEqual(cats["Alcohol"]["rule_count"], 1)
        self.assertTrue(cats["Groceries"]["has_budget_rule"])
        self.assertEqual(cats["Groceries"]["weekly_limit"], 200.0)
        self.assertEqual(cats["Travel"]["amortize_period"], "annual")
        self.assertEqual(cats["Travel"]["budget_period"], "annual")
        self.assertTrue(cats["General"]["is_system"])
        self.assertTrue(cats["Transfers"]["is_system"])
        self.assertTrue(cats["Income"]["is_system"])
        self.assertTrue(cats["Debt & Loans"]["is_system"])

        # Debt & Loans is protected as a system category yet still exposed in GET /runway categories
        debt_id = cats["Debt & Loans"]["id"]
        food_id = cats["Food"]["id"]
        self.assertEqual(
            self.client.patch(f"/categories/{debt_id}", json={"name": "Debts"}).status_code,
            400,
        )
        self.assertEqual(
            self.client.patch(f"/categories/{debt_id}", json={"parent_id": food_id}).status_code,
            400,
        )
        self.assertEqual(
            self.client.patch(f"/categories/{debt_id}", json={"is_active": False}).status_code,
            400,
        )
        self.assertEqual(
            self.client.delete(f"/categories/{debt_id}").status_code,
            400,
        )

        runway_cats = self.client.get("/runway").json()["categories"]
        self.assertIn("Debt & Loans", runway_cats)
        self.assertNotIn("General", runway_cats)
        self.assertNotIn("Transfers", runway_cats)
        self.assertNotIn("Income", runway_cats)

    def test_create_category_validation_and_duplicate_guards(self):
        cats = {c["name"]: c for c in self.client.get("/categories").json()}

        # Create root category
        root_res = self.client.post(
            "/categories",
            json={"name": "Pets", "amortize_period": None},
        )
        self.assertEqual(root_res.status_code, 201)
        pets = root_res.json()
        self.assertIsNone(pets["parent_id"])

        # Create subclass under Pets
        child_res = self.client.post(
            "/categories",
            json={"name": "Vet Care", "parent_id": pets["id"], "amortize_period": "annual"},
        )
        self.assertEqual(child_res.status_code, 201)
        vet = child_res.json()
        self.assertEqual(vet["parent_name"], "Pets")
        self.assertEqual(vet["amortize_period"], "annual")

        # Duplicate name rejected (case-insensitive)
        dup_res = self.client.post("/categories", json={"name": "pets"})
        self.assertEqual(dup_res.status_code, 409)

        # 3-level nesting rejected
        grandchild_res = self.client.post(
            "/categories",
            json={"name": "Dog Food", "parent_id": vet["id"]},
        )
        self.assertEqual(grandchild_res.status_code, 400)

        # Nesting under system category rejected
        sys_child = self.client.post(
            "/categories",
            json={"name": "Misc Fee", "parent_id": cats["General"]["id"]},
        )
        self.assertEqual(sys_child.status_code, 400)

    def test_rename_cascades_across_all_tables_and_reparent_updates_rollups(self):
        cats = {c["name"]: c for c in self.client.get("/categories").json()}
        food_id = cats["Food"]["id"]
        shopping_id = cats["Shopping"]["id"]

        created = self.client.post(
            "/categories",
            json={"name": "Coffee Beans", "parent_id": food_id},
        ).json()

        today = date.today()
        tx = Transaction(
            transaction_date=today,
            account_id=self.acc.id,
            amount=-36.0,
            currency="NZD",
            merchant="Flight Coffee",
            category="Coffee Beans",
            raw_hash="rename-tx-1",
        )
        self.db.add(tx)
        self.db.flush()
        rule = CategorizationRule(
            name="Coffee Beans Rule",
            description_pattern="Flight Coffee",
            match_type="contains",
            target_category="Coffee Beans",
        )
        budget = BudgetRule(
            category="Coffee Beans",
            weekly_limit=25.0,
            monthly_limit=108.33,
            budget_period="monthly",
            is_active=True,
        )
        prod = Product(name="Flight Bomber 1kg", category="Coffee Beans")
        self.db.add_all([rule, budget, prod])
        self.db.flush()
        rec = Receipt(
            receipt_date=today,
            merchant="Flight Coffee",
            total_amount=36.0,
            currency="NZD",
            status="matched",
            transaction_id=tx.id,
        )
        self.db.add(rec)
        self.db.flush()
        item = ReceiptItem(
            receipt_id=rec.id,
            product_id=prod.id,
            item_name="Flight Bomber 1kg",
            unit_price=36.0,
            total_price=36.0,
            category="Coffee Beans",
        )
        self.db.add(item)
        self.db.commit()

        # Rename Coffee Beans -> Espresso and reparent under Shopping
        patch_res = self.client.patch(
            f"/categories/{created['id']}",
            json={"name": "Espresso", "parent_id": shopping_id},
        )
        self.assertEqual(patch_res.status_code, 200)
        self.assertEqual(patch_res.json()["name"], "Espresso")
        self.assertEqual(patch_res.json()["parent_name"], "Shopping")

        self.db.expire_all()
        self.assertEqual(self.db.get(Transaction, tx.id).category, "Espresso")
        self.assertEqual(self.db.get(CategorizationRule, rule.id).target_category, "Espresso")
        self.assertEqual(self.db.get(BudgetRule, budget.id).category, "Espresso")
        self.assertEqual(self.db.get(Product, prod.id).category, "Espresso")
        self.assertEqual(self.db.get(ReceiptItem, item.id).category, "Espresso")

        # Dynamic parent/child reflects in base breakdown and include_subclasses filter
        base_bd = self.client.get("/budgets/breakdown?period=month&group_by=base").json()
        base_cats = {c["category"]: c["total_spend"] for c in base_bd["categories"]}
        self.assertEqual(base_cats.get("Shopping"), 36.0)
        self.assertNotIn("Food", base_cats)

        sub_txs = self.client.get(
            "/transactions?category=Shopping&period=month&include_subclasses=true"
        ).json()
        self.assertEqual(len(sub_txs), 1)
        self.assertEqual(sub_txs[0]["id"], tx.id)

    def test_category_amortize_period_without_budget_rule_and_budget_sync(self):
        today = date.today()
        self.db.add(
            AssetSnapshot(
                account_id=self.acc.id,
                snapshot_date=today,
                balance=10400.0,
                currency="NZD",
                exchange_rate=1.0,
                balance_nzd=10400.0,
                source="manual",
            )
        )
        self.db.add(
            Transaction(
                transaction_date=today,
                account_id=self.acc.id,
                amount=-1040.0,
                currency="NZD",
                merchant="Mechanics R Us",
                category="Vehicle",
                raw_hash="amort-veh-1",
            )
        )
        self.db.commit()

        cats = {c["name"]: c for c in self.client.get("/categories").json()}
        veh_id = cats["Vehicle"]["id"]

        # Toggle 52w annual spread on Vehicle WITHOUT creating a BudgetRule
        patch_res = self.client.patch(
            f"/categories/{veh_id}",
            json={"amortize_period": "annual"},
        )
        self.assertEqual(patch_res.status_code, 200)
        self.assertEqual(patch_res.json()["amortize_period"], "annual")
        self.assertFalse(patch_res.json()["has_budget_rule"])

        # Weekly breakdown amortizes $1,040 / 52 = $20/wk while displaying 52w total $1,040
        week_bd = self.client.get("/budgets/breakdown?period=week").json()
        self.assertAlmostEqual(week_bd["total_spend"], 20.0, places=2)
        veh_row = next(c for c in week_bd["categories"] if c["category"] == "Vehicle")
        self.assertEqual(veh_row["budget_period"], "annual")
        self.assertAlmostEqual(veh_row["total_spend"], 1040.0, places=2)
        self.assertIsNone(veh_row["budget_limit"])

        # Average burn and Runway reflect the 52-week spread ($20/wk)
        avg = self.client.get("/budgets/average-burn?lookback_weeks=4").json()
        veh_burn = next(c for c in avg["categories"] if c["category"] == "Vehicle")
        self.assertEqual(veh_burn["budget_period"], "annual")
        self.assertAlmostEqual(veh_burn["variable_weekly_burn"], 20.0, places=2)

        runway = self.client.get("/runway").json()
        self.assertAlmostEqual(runway["breakdown"]["weekly_burn_rate"], 20.0, places=2)

        # Creating a monthly BudgetRule for Vehicle clears Category.amortize_period in sync
        b_res = self.client.post(
            "/budgets",
            json={"category": "Vehicle", "monthly_limit": 200.0, "budget_period": "monthly"},
        )
        self.assertEqual(b_res.status_code, 200)
        cats_after = {c["name"]: c for c in self.client.get("/categories").json()}
        self.assertIsNone(cats_after["Vehicle"]["amortize_period"])
        self.assertEqual(cats_after["Vehicle"]["budget_period"], "monthly")

        # Switching BudgetRule back to annual sets Category.amortize_period = "annual"
        b_ann = self.client.post(
            "/budgets",
            json={"category": "Vehicle", "annual_limit": 1200.0, "budget_period": "annual"},
        )
        self.assertEqual(b_ann.status_code, 200)
        cats_ann = {c["name"]: c for c in self.client.get("/categories").json()}
        self.assertEqual(cats_ann["Vehicle"]["amortize_period"], "annual")

    def test_delete_category_reassigns_and_promotes_children_without_reseeding(self):
        cats = {c["name"]: c for c in self.client.get("/categories").json()}
        alc = cats["Alcohol"]
        gen = cats["General"]

        # Cannot delete system category
        del_sys = self.client.delete(f"/categories/{gen['id']}")
        self.assertEqual(del_sys.status_code, 400)

        today = date.today()
        tx = Transaction(
            transaction_date=today,
            account_id=self.acc.id,
            amount=-45.0,
            currency="NZD",
            merchant="Super Liquor",
            category="Alcohol",
            raw_hash="del-alc-tx",
        )
        rule = CategorizationRule(
            name="Super Liquor Rule",
            description_pattern="Super Liquor",
            match_type="contains",
            target_category="Alcohol",
        )
        budget = BudgetRule(
            category="Alcohol",
            weekly_limit=30.0,
            monthly_limit=130.0,
            is_active=True,
        )
        self.db.add_all([tx, rule, budget])
        self.db.commit()

        # Deleting subclass defaults reassignment to parent (Food) and deletes its BudgetRule
        del_res = self.client.delete(f"/categories/{alc['id']}")
        self.assertEqual(del_res.status_code, 200)
        payload = del_res.json()
        self.assertEqual(payload["deleted_name"], "Alcohol")
        self.assertEqual(payload["reassigned_to"], "Food")
        self.assertEqual(payload["transactions_reassigned"], 1)
        self.assertEqual(payload["rules_reassigned"], 1)
        self.assertEqual(payload["children_promoted"], 0)

        self.db.expire_all()
        self.assertEqual(self.db.get(Transaction, tx.id).category, "Food")
        self.assertEqual(self.db.get(CategorizationRule, rule.id).target_category, "Food")
        self.assertIsNone(
            self.db.scalar(select(BudgetRule).where(BudgetRule.category == "Alcohol"))
        )

        # Subsequent GET /categories does NOT re-seed deleted default Alcohol
        names_after = {c["name"] for c in self.client.get("/categories").json()}
        self.assertNotIn("Alcohol", names_after)

        # Deleting a root promotes its children to top level and reassigns its txs to General
        housing_id = cats["Housing"]["id"]
        del_root = self.client.delete(f"/categories/{housing_id}")
        self.assertEqual(del_root.status_code, 200)
        self.assertEqual(del_root.json()["reassigned_to"], "General")
        self.assertEqual(del_root.json()["children_promoted"], 2)

        cats_final = {c["name"]: c for c in self.client.get("/categories").json()}
        self.assertIsNone(cats_final["Rent"]["parent_id"])
        self.assertIsNone(cats_final["Household"]["parent_id"])

    def test_base_class_budget_limit_rollup_and_annual_root_dedup(self):
        today = date.today()
        self.client.get("/categories")  # Seed hierarchy

        # Configure Food ($100/wk) and Groceries ($200/wk) budgets
        self.client.post("/budgets", json={"category": "Food", "weekly_limit": 100.0})
        self.client.post("/budgets", json={"category": "Groceries", "weekly_limit": 200.0})

        # Mark Leisure (root) as annual; Gaming (child) stays non-annual
        cats = {c["name"]: c for c in self.client.get("/categories").json()}
        self.client.patch(f"/categories/{cats['Leisure']['id']}", json={"amortize_period": "annual"})

        self.db.add_all(
            [
                Transaction(
                    transaction_date=today,
                    account_id=self.acc.id,
                    amount=-120.0,
                    currency="NZD",
                    merchant="Pak n Save",
                    category="Groceries",
                    raw_hash="base-groc",
                ),
                Transaction(
                    transaction_date=today,
                    account_id=self.acc.id,
                    amount=-40.0,
                    currency="NZD",
                    merchant="Local Cafe",
                    category="Dining",
                    raw_hash="base-dine",
                ),
                Transaction(
                    transaction_date=today,
                    account_id=self.acc.id,
                    amount=-520.0,
                    currency="NZD",
                    merchant="Steam",
                    category="Gaming",
                    raw_hash="base-gaming",
                ),
                Transaction(
                    transaction_date=today - timedelta(days=90),
                    account_id=self.acc.id,
                    amount=-520.0,
                    currency="NZD",
                    merchant="Concert",
                    category="Leisure",
                    raw_hash="base-leisure",
                ),
            ]
        )
        self.db.commit()

        base_week = self.client.get("/budgets/breakdown?period=week&group_by=base").json()
        by_cat = {c["category"]: c for c in base_week["categories"]}

        # Groceries & Dining do not emit separate $0 rows in base mode
        self.assertNotIn("Groceries", by_cat)
        self.assertNotIn("Dining", by_cat)
        self.assertNotIn("Gaming", by_cat)

        # Food aggregates both spend (120 + 40 = 160) AND summed weekly limit (100 + 200 = 300)
        self.assertAlmostEqual(by_cat["Food"]["total_spend"], 160.0, places=2)
        self.assertAlmostEqual(by_cat["Food"]["budget_limit"], 300.0, places=2)

        # Leisure (annual root) combines Leisure + Gaming 365d spend (520 + 520 = 1040 -> $20/wk)
        self.assertEqual(by_cat["Leisure"]["budget_period"], "annual")
        self.assertAlmostEqual(by_cat["Leisure"]["total_spend"], 1040.0, places=2)
        self.assertAlmostEqual(base_week["total_spend"], 180.0, places=2)

    def test_startup_normalizer_preserves_custom_db_categories_and_regex_fixes(self):
        self.client.get("/categories")
        self.client.post("/categories", json={"name": "Pets"})

        tx = Transaction(
            transaction_date=date.today(),
            account_id=self.acc.id,
            amount=-55.0,
            currency="NZD",
            merchant="Animates",
            category="Pets",
            raw_hash="custom-pets-tx",
            tags="[]",
        )
        self.db.add(tx)
        self.db.commit()

        updated = normalize_existing_legacy_categories(self.db)
        self.assertEqual(updated, 0)
        self.db.refresh(tx)
        self.assertEqual(tx.category, "Pets")

        # Regex & line-item bug regressions
        self.assertEqual(auto_categorize("Tinapay Atbp"), "General")
        self.assertEqual(auto_categorize("Salami By Microv"), "General")
        self.assertEqual(auto_categorize("The Picasso Botique"), "Travel")
        self.assertEqual(auto_categorize("Super Liquor"), "Alcohol")
        self.assertEqual(classify_line_item("Copper & Oak Pant").category, "Clothing")
        self.assertEqual(classify_line_item("Tefal Nonstick Frypan").category, "Household")

    def test_http_contract_schema_fields_and_error_mapping(self):
        cats = self.client.get("/categories").json()
        expected_fields = {
            "id",
            "name",
            "parent_id",
            "parent_name",
            "is_system",
            "is_active",
            "amortize_period",
            "sort_order",
            "transaction_count",
            "rule_count",
            "child_count",
            "has_budget_rule",
            "budget_period",
            "weekly_limit",
            "monthly_limit",
            "annual_limit",
        }
        self.assertEqual(set(cats[0].keys()), expected_fields)
        self.assertEqual(self.client.patch("/categories/999999", json={"name": "Nope"}).status_code, 404)
        self.assertEqual(self.client.delete("/categories/999999").status_code, 404)
        self.assertEqual(self.client.post("/categories", json={"name": "Ghost", "parent_id": 999999}).status_code, 404)
        self.assertEqual(self.client.post("/categories", json={"name": "FOOD"}).status_code, 409)
        self.assertEqual(
            self.client.post("/categories", json={"name": "Transfers", "amortize_period": "annual"}).status_code,
            400,
        )

    def test_http_delete_reports_splits_reassigned(self):
        cats = self.client.get("/categories").json()
        food_id = next(c["id"] for c in cats if c["name"] == "Food")
        created = self.client.post("/categories", json={"name": "Snacks", "parent_id": food_id}).json()
        parent_tx = Transaction(
            transaction_date=date.today(),
            account_id=self.acc.id,
            amount=-20.0,
            currency="NZD",
            merchant="Corner Dairy",
            category="General",
            raw_hash="http-split-del",
        )
        self.db.add(parent_tx)
        self.db.flush()
        split = TransactionSplit(transaction_id=parent_tx.id, amount=-12.0, category="Snacks")
        self.db.add(split)
        self.db.commit()

        del_res = self.client.delete(f"/categories/{created['id']}")
        self.assertEqual(del_res.status_code, 200)
        body = del_res.json()
        self.assertEqual(body["splits_reassigned"], 1)
        self.assertEqual(body["reassigned_to"], "Food")
        self.db.expire_all()
        self.assertEqual(self.db.get(TransactionSplit, split.id).category, "Food")


if __name__ == "__main__":
    unittest.main()
