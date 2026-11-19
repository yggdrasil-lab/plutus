"""Unit tests for Receipt parsing, line items, cross-domain telemetry, and transaction matching."""

from datetime import date, timedelta
import io
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
from app.models import Account, Receipt, ReceiptItem, Transaction
from app.services.receipt_service import (
    classify_line_item,
    compute_product_statistics,
    match_receipt_to_transaction,
    reconcile_unlinked_receipts,
)

TEST_DB_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class TestReceiptsRouter(unittest.TestCase):
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

    def test_classify_line_item_heuristics(self):
        # 1. Skincare (Area 02-Skin)
        cat, dom, ptype, vol = classify_line_item("Geek & Gorgeous C-Glow 15% Vitamin C 30ml")
        self.assertEqual(cat, "Skincare")
        self.assertEqual(dom, "02-skin")
        self.assertEqual(ptype, "vitamin_c_serum")
        self.assertEqual(vol, "30ml")

        # 2. Supplements (Area 01-Health)
        cat, dom, ptype, vol = classify_line_item("Optimum Nutrition Gold Standard 100% Whey 1kg")
        self.assertEqual(cat, "Supplements")
        self.assertEqual(dom, "01-health")
        self.assertEqual(ptype, "protein_powder")
        self.assertEqual(vol, "1kg")

        # 3. Hardware (Area 20-Home)
        cat, dom, ptype, vol = classify_line_item("Anker USB-C Fast Charger Adapter")
        self.assertEqual(cat, "Household")
        self.assertEqual(dom, "20-home")
        self.assertEqual(ptype, "appliance")

    def test_create_receipt_and_auto_match(self):
        # Seed an existing credit card transaction
        tx = Transaction(
            transaction_date=date(2026, 11, 4),
            account_id=self.acc.id,
            amount=-34.00,
            currency="NZD",
            merchant="Chemist Warehouse Albany",
            category="Health",
            raw_hash="hash_cw_1",
        )
        self.db.add(tx)
        self.db.commit()
        self.db.refresh(tx)

        # Post receipt with matching total and merchant within +/- 3 days
        res = self.client.post(
            "/receipts",
            json={
                "receipt_date": "2026-11-04",
                "merchant": "Chemist Warehouse",
                "total_amount": 34.00,
                "tax_amount": 4.43,
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
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data["status"], "matched")
        self.assertEqual(data["transaction_id"], tx.id)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["domain"], "02-skin")
        self.assertEqual(data["items"][0]["product_type"], "vitamin_c_serum")

    def test_reconcile_receipt_first_statement_later(self):
        # 1. Receipt uploaded first (no transaction in ledger yet)
        res = self.client.post(
            "/receipts",
            json={
                "receipt_date": "2026-11-05",
                "merchant": "Farmers Beauty",
                "total_amount": 89.00,
                "items": [
                    {
                        "item_name": "La Roche-Posay Anthelios SPF50 50ml",
                        "quantity": 1.0,
                        "unit_price": 89.00,
                        "total_price": 89.00,
                    }
                ],
            },
        )
        self.assertEqual(res.status_code, 201)
        r_data = res.json()
        self.assertEqual(r_data["status"], "unlinked")
        self.assertIsNone(r_data["transaction_id"])
        receipt_id = r_data["id"]

        # 2. Later, credit card statement is imported containing this transaction
        tx = Transaction(
            transaction_date=date(2026, 11, 6),
            account_id=self.acc.id,
            amount=-89.00,
            currency="NZD",
            merchant="Farmers Albany",
            category="Shopping",
            raw_hash="hash_farmers_1",
        )
        self.db.add(tx)
        self.db.commit()

        # 3. Trigger reconciliation
        recon_res = self.client.post("/receipts/reconcile")
        self.assertEqual(recon_res.status_code, 200)
        self.assertEqual(recon_res.json()["matched_count"], 1)

        # 4. Receipt is now matched
        get_res = self.client.get(f"/receipts/{receipt_id}")
        self.assertEqual(get_res.status_code, 200)
        self.assertEqual(get_res.json()["status"], "matched")
        self.assertEqual(get_res.json()["transaction_id"], tx.id)

    def test_compute_product_statistics_longevity(self):
        # Two purchases of Vitamin C serum 60 days apart
        r1 = Receipt(
            receipt_date=date(2026, 8, 1),
            merchant="Chemist Warehouse",
            total_amount=34.00,
        )
        self.db.add(r1)
        self.db.flush()
        it1 = ReceiptItem(
            receipt_id=r1.id,
            item_name="Geek & Gorgeous C-Glow 15% Vitamin C 30ml",
            brand="Geek & Gorgeous",
            quantity=1.0,
            unit_price=34.00,
            total_price=34.00,
            category="Skincare",
            domain="02-skin",
            product_type="vitamin_c_serum",
            volume_ml="30ml",
        )
        self.db.add(it1)

        r2 = Receipt(
            receipt_date=date(2026, 10, 1),
            merchant="Chemist Warehouse",
            total_amount=34.00,
        )
        self.db.add(r2)
        self.db.flush()
        it2 = ReceiptItem(
            receipt_id=r2.id,
            item_name="Geek & Gorgeous C-Glow 15% Vitamin C 30ml",
            brand="Geek & Gorgeous",
            quantity=1.0,
            unit_price=34.00,
            total_price=34.00,
            category="Skincare",
            domain="02-skin",
            product_type="vitamin_c_serum",
            volume_ml="30ml",
        )
        self.db.add(it2)
        self.db.commit()

        # Query statistics
        res = self.client.get("/receipts/statistics/products?domain=02-skin")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["total_products"], 1)
        self.assertEqual(data["total_spend"], 68.00)

        prod = data["products"][0]
        self.assertEqual(prod["item_name"], "Geek & Gorgeous C-Glow 15% Vitamin C 30ml")
        self.assertEqual(prod["purchase_count"], 2)
        self.assertEqual(prod["total_spent"], 68.00)
        self.assertEqual(prod["avg_days_between_purchases"], 61.0)
        self.assertEqual(prod["estimated_depletion_date"], "2026-12-01")

    @staticmethod
    def _make_test_png_bytes(width: int = 2000, height: int = 2000) -> bytes:
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (width, height), color=(255, 255, 255))
        draw = ImageDraw.Draw(img)
        draw.rectangle([50, 50, width - 50, height - 50], outline=(0, 0, 0), width=4)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_upload_receipt_image_processed_compresses_and_serves(self):
        raw_png = self._make_test_png_bytes(2000, 2000)
        mock_parsed = {
            "merchant": "Chemist Warehouse",
            "receipt_date": "2026-11-19",
            "total_amount": 34.00,
            "tax_amount": 4.43,
            "items": [
                {
                    "item_name": "Geek & Gorgeous C-Glow Vitamin C 30ml",
                    "brand": "Geek & Gorgeous",
                    "quantity": 1.0,
                    "unit_price": 34.00,
                    "total_price": 34.00,
                }
            ],
        }

        with patch("app.routers.receipts.parse_receipt_with_llm", return_value=mock_parsed) as mock_llm:
            res = self.client.post(
                "/receipts/upload",
                files={"file": ("receipt.png", raw_png, "image/png")},
            )
            mock_llm.assert_called_once()

        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertTrue(data["is_processed"])
        self.assertTrue(data["has_image"])
        self.assertEqual(data["merchant"], "Chemist Warehouse")
        self.assertEqual(data["total_amount"], 34.0)
        self.assertEqual(data["image_mime_type"], "image/webp")
        self.assertEqual(len(data["items"]), 1)

        img_res = self.client.get(f"/receipts/{data['id']}/image")
        self.assertEqual(img_res.status_code, 200)
        self.assertEqual(img_res.headers["content-type"], "image/webp")
        self.assertLess(len(img_res.content), len(raw_png))

    def test_upload_unprocessed_preserves_original_until_reprocessed_then_delete(self):
        raw_png = self._make_test_png_bytes(1800, 1800)

        with patch("app.routers.receipts.parse_receipt_with_llm", return_value=None):
            res = self.client.post(
                "/receipts/upload",
                files={"file": ("IMG_1234.png", raw_png, "image/png")},
            )

        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertFalse(data["is_processed"])
        self.assertTrue(data["has_image"])
        self.assertEqual(data["image_mime_type"], "image/png")

        # Original bytes preserved verbatim while unprocessed
        img_res = self.client.get(f"/receipts/{data['id']}/image")
        self.assertEqual(img_res.status_code, 200)
        self.assertEqual(img_res.content, raw_png)

        # Reprocess succeeds and compresses the stored image
        mock_parsed = {
            "merchant": "Woolworths",
            "receipt_date": "2026-11-19",
            "total_amount": 12.50,
            "items": [
                {
                    "item_name": "Organic Milk 2L",
                    "quantity": 1.0,
                    "unit_price": 12.50,
                    "total_price": 12.50,
                }
            ],
        }
        with patch("app.routers.receipts.parse_receipt_with_llm", return_value=mock_parsed):
            re_res = self.client.post(f"/receipts/{data['id']}/reprocess")

        self.assertEqual(re_res.status_code, 200)
        re_data = re_res.json()
        self.assertTrue(re_data["is_processed"])
        self.assertEqual(re_data["merchant"], "Woolworths")
        self.assertEqual(re_data["image_mime_type"], "image/webp")

        # Delete removes both receipt and stored image
        del_res = self.client.delete(f"/receipts/{data['id']}")
        self.assertEqual(del_res.status_code, 204)
        self.assertEqual(self.client.get(f"/receipts/{data['id']}/image").status_code, 404)

    def test_receipt_allows_negative_discount_line_items(self):
        res = self.client.post(
            "/receipts",
            json={
                "receipt_date": "2026-11-17",
                "merchant": "Costco Wholesale",
                "total_amount": 27.99,
                "items": [
                    {
                        "item_name": "Coca Cola Zero 36P",
                        "quantity": 1.0,
                        "unit_price": 34.99,
                        "total_price": 34.99,
                    },
                    {
                        "item_name": "TPD Coca Cola No S",
                        "quantity": 1.0,
                        "unit_price": -7.00,
                        "total_price": -7.00,
                    },
                ],
            },
        )
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(data["items"][1]["total_price"], -7.0)
        list_res = self.client.get("/receipts")
        self.assertEqual(list_res.status_code, 200)
