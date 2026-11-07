"""Unit tests for Plutus Product catalog, tags, notes, and price fluctuation tracking."""

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
from app.models import Product, Receipt, ReceiptItem

TEST_DB_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class TestProductsRouter(unittest.TestCase):
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

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_auto_catalog_product_from_receipt_and_track_price_change(self):
        # 1. First purchase: $34.00
        res1 = self.client.post(
            "/receipts",
            json={
                "receipt_date": "2026-08-01",
                "merchant": "Chemist Warehouse Albany",
                "total_amount": 34.00,
                "items": [
                    {
                        "item_name": "Geek & Gorgeous C-Glow Vitamin C 30ml",
                        "brand": "Geek & Gorgeous",
                        "quantity": 1.0,
                        "unit_price": 34.00,
                        "total_price": 34.00,
                    }
                ],
            },
        )
        self.assertEqual(res1.status_code, 201)

        # 2. Verify product created in catalog
        prod_res1 = self.client.get("/products")
        self.assertEqual(prod_res1.status_code, 200)
        data1 = prod_res1.json()
        self.assertEqual(data1["total_count"], 1)
        self.assertIn("serum", data1["all_tags"])
        self.assertIn("vitamin-c", data1["all_tags"])

        prod = data1["products"][0]
        self.assertEqual(prod["name"], "Geek & Gorgeous C-Glow Vitamin C 30ml")
        self.assertEqual(prod["brand"], "Geek & Gorgeous")
        self.assertEqual(prod["latest_price"], 34.00)
        self.assertIsNone(prod["price_change_latest"])
        self.assertEqual(prod["purchase_count"], 1)
        prod_id = prod["id"]

        # 3. Second purchase: Price increased to $39.50 (+5.50)
        res2 = self.client.post(
            "/receipts",
            json={
                "receipt_date": "2026-10-15",
                "merchant": "Farmers Albany",
                "total_amount": 39.50,
                "items": [
                    {
                        "item_name": "Geek & Gorgeous C-Glow Vitamin C 30ml",
                        "brand": "Geek & Gorgeous",
                        "quantity": 1.0,
                        "unit_price": 39.50,
                        "total_price": 39.50,
                    }
                ],
            },
        )
        self.assertEqual(res2.status_code, 201)

        # 4. Check product detail and price change
        prod_res2 = self.client.get(f"/products/{prod_id}")
        self.assertEqual(prod_res2.status_code, 200)
        prod_detail = prod_res2.json()
        self.assertEqual(prod_detail["purchase_count"], 2)
        self.assertEqual(prod_detail["latest_price"], 39.50)
        self.assertEqual(prod_detail["lowest_price"], 34.00)
        self.assertEqual(prod_detail["highest_price"], 39.50)
        self.assertEqual(prod_detail["price_change_latest"], 5.50)
        self.assertEqual(len(prod_detail["price_history"]), 2)
        self.assertEqual(prod_detail["price_history"][0]["unit_price"], 34.00)
        self.assertEqual(prod_detail["price_history"][1]["unit_price"], 39.50)

    def test_update_product_notes_tags_and_filter(self):
        # 1. Create a product directly
        create_res = self.client.post(
            "/products",
            json={
                "name": "Whey Isolate Protein Powder 1kg",
                "brand": "Optimum Nutrition",
                "category": "Supplements",
                "domain": "01-health",
                "tags": ["supplements", "protein", "post-workout"],
                "volume_ml": "1kg",
                "notes": "Double Rich Chocolate flavor, dissolves very easily with cold milk.",
                "rating": 5,
                "is_favorite": True,
            },
        )
        self.assertEqual(create_res.status_code, 201)
        created = create_res.json()
        prod_id = created["id"]
        self.assertEqual(created["rating"], 5)
        self.assertTrue(created["is_favorite"])
        self.assertEqual(created["notes"], "Double Rich Chocolate flavor, dissolves very easily with cold milk.")

        # 2. Update notes and add another tag
        patch_res = self.client.patch(
            f"/products/{prod_id}",
            json={
                "notes": "Updated note: Double Rich Chocolate flavor, best with 300ml almond milk.",
                "tags": ["supplements", "protein", "post-workout", "dairy"],
                "rating": 4,
            },
        )
        self.assertEqual(patch_res.status_code, 200)
        updated = patch_res.json()
        self.assertEqual(updated["rating"], 4)
        self.assertEqual(updated["notes"], "Updated note: Double Rich Chocolate flavor, best with 300ml almond milk.")
        self.assertIn("dairy", updated["tags"])

        # 3. Filter by tag
        filter_res = self.client.get("/products?tag=protein")
        self.assertEqual(filter_res.status_code, 200)
        self.assertEqual(filter_res.json()["total_count"], 1)

        # 4. Filter by non-existent tag
        empty_res = self.client.get("/products?tag=skincare")
        self.assertEqual(empty_res.status_code, 200)
        self.assertEqual(empty_res.json()["total_count"], 0)

        # 5. Search query matching notes
        search_res = self.client.get("/products?search=almond")
        self.assertEqual(search_res.status_code, 200)
        self.assertEqual(search_res.json()["total_count"], 1)
