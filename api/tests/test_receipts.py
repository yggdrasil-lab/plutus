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
    extract_tags_for_line_item,
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
        # 1. Personal Care / Skincare (Area 02-Skin)
        cat, dom, ptype, vol = classify_line_item("Geek & Gorgeous C-Glow 15% Vitamin C 30ml")
        self.assertEqual(cat, "Personal Care")
        self.assertEqual(dom, "02-skin")
        self.assertEqual(ptype, "vitamin_c_serum")
        self.assertEqual(vol, "30ml")

        # 2. Supplements (Area 01-Health)
        cat, dom, ptype, vol = classify_line_item("Optimum Nutrition Gold Standard 100% Whey 1kg")
        self.assertEqual(cat, "Supplements")
        self.assertEqual(dom, "01-health")
        self.assertEqual(ptype, "protein_powder")
        self.assertEqual(vol, "1kg")

        # 3. Hardware / Tech (Area 20-Home)
        cat, dom, ptype, vol = classify_line_item("Anker USB-C Fast Charger Adapter")
        self.assertEqual(cat, "Tech")
        self.assertEqual(dom, "20-home")
        self.assertEqual(ptype, "tech")

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

    def test_upload_folds_discount_into_target_item_via_jev(self):
        raw_png = self._make_test_png_bytes(400, 400)
        mock_parsed = {
            "merchant": "Costco Wholesale",
            "receipt_date": "2026-11-17",
            "total_amount": 42.98,
            "items": [
                {
                    "item_name": "Lamingtons 16 Pack",
                    "quantity": 1.0,
                    "unit_price": 14.99,
                    "total_price": 14.99,
                },
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
        }

        with (
            patch("app.routers.receipts.parse_receipt_with_llm", return_value=mock_parsed),
            patch("app.services.receipt_service.call_jev", return_value=('{"target_index": "1"}', 0.05)),
        ):
            res = self.client.post(
                "/receipts/upload",
                files={"file": ("costco.png", raw_png, "image/png")},
            )

        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(len(data["items"]), 2)
        coke = next(it for it in data["items"] if "Coca Cola" in it["item_name"])
        self.assertEqual(coke["unit_price"], 34.99)
        self.assertEqual(coke["discount_amount"], 7.0)
        self.assertEqual(coke["total_price"], 27.99)
        self.assertIn("TPD Coca Cola No S", coke["notes"])

    def test_apply_receipt_discounts_heuristic_fallback_when_jev_offline(self):
        from app.services.receipt_service import apply_receipt_discounts_with_jev

        items = [
            {"item_name": "CP Wonton Noodle", "quantity": 1.0, "unit_price": 29.99, "total_price": 29.99},
            {"item_name": "Coca Cola Zero 36P", "quantity": 1.0, "unit_price": 34.99, "total_price": 34.99},
            {"item_name": "TPD Coca Cola No S", "quantity": 1.0, "unit_price": -7.00, "total_price": -7.00},
        ]
        with patch("app.services.receipt_service.call_jev", return_value=(None, 0.0)):
            resolved = apply_receipt_discounts_with_jev(items)

        self.assertEqual(len(resolved), 2)
        self.assertEqual(resolved[1]["unit_price"], 34.99)
        self.assertEqual(resolved[1]["discount_amount"], 7.0)
        self.assertEqual(resolved[1]["total_price"], 27.99)

    def test_matched_receipt_splits_transaction_categories_in_breakdown_and_burn(self):
        today = date.today()
        tx = Transaction(
            transaction_date=today,
            account_id=self.acc.id,
            amount=-154.93,
            currency="NZD",
            merchant="Costco Wholesale",
            category="General",
            raw_hash="hash_costco_split",
        )
        self.db.add(tx)
        self.db.commit()
        self.db.refresh(tx)

        res = self.client.post(
            "/receipts",
            json={
                "receipt_date": today.isoformat(),
                "merchant": "Costco Wholesale",
                "total_amount": 154.93,
                "items": [
                    {
                        "item_name": "CP Wonton Noodle",
                        "quantity": 1.0,
                        "unit_price": 29.99,
                        "total_price": 29.99,
                        "category": "Groceries",
                    },
                    {
                        "item_name": "Lamingtons 16 Pack",
                        "quantity": 1.0,
                        "unit_price": 14.99,
                        "total_price": 14.99,
                        "category": "Groceries",
                    },
                    {
                        "item_name": "Coca Cola Zero 36P",
                        "quantity": 1.0,
                        "unit_price": 34.99,
                        "discount_amount": 7.0,
                        "total_price": 27.99,
                        "category": "Groceries",
                    },
                    {
                        "item_name": "Body Glove Slide",
                        "quantity": 1.0,
                        "unit_price": 26.99,
                        "total_price": 26.99,
                        "category": "Clothing",
                    },
                    {
                        "item_name": "Copper & Oak Pant",
                        "quantity": 1.0,
                        "unit_price": 37.98,
                        "total_price": 37.98,
                        "category": "Clothing",
                    },
                    {
                        "item_name": "Puma Mens Liner 6P",
                        "quantity": 1.0,
                        "unit_price": 16.99,
                        "total_price": 16.99,
                        "category": "Clothing",
                    },
                ],
            },
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["status"], "matched")

        breakdown = self.client.get("/budgets/breakdown?period=month")
        self.assertEqual(breakdown.status_code, 200)
        b_data = breakdown.json()
        self.assertEqual(b_data["total_spend"], 154.93)
        cats = {c["category"]: c["total_spend"] for c in b_data["categories"]}
        self.assertEqual(cats.get("Groceries"), 72.97)
        self.assertEqual(cats.get("Clothing"), 81.96)
        self.assertNotIn("General", cats)

        burn = self.client.get("/budgets/average-burn?lookback_weeks=4")
        self.assertEqual(burn.status_code, 200)
        burn_cats = {c["category"]: c["variable_weekly_burn"] for c in burn.json()["categories"]}
        self.assertAlmostEqual(burn_cats["Groceries"], round(72.97 / 4.0, 2))
        self.assertAlmostEqual(burn_cats["Clothing"], round(81.96 / 4.0, 2))
        self.assertNotIn("General", burn_cats)

        g_txs = self.client.get("/transactions?category=Groceries&period=month")
        self.assertEqual(g_txs.status_code, 200)
        self.assertEqual(len(g_txs.json()), 1)
        self.assertEqual(g_txs.json()[0]["amount"], -72.97)
        self.assertTrue(g_txs.json()[0]["is_receipt_split"])

        c_txs = self.client.get("/transactions?category=Clothing&period=month")
        self.assertEqual(c_txs.status_code, 200)
        self.assertEqual(len(c_txs.json()), 1)
        self.assertEqual(c_txs.json()[0]["amount"], -81.96)
        self.assertTrue(c_txs.json()[0]["is_receipt_split"])

        all_txs = self.client.get("/transactions?period=month")
        self.assertEqual(all_txs.status_code, 200)
        self.assertEqual(all_txs.json()[0]["amount"], -154.93)
        self.assertFalse(all_txs.json()[0]["is_receipt_split"])

        # OOP category inheritance: leaf reports expose parent_category, group_by=base rolls up
        leaf_parents = {c["category"]: c["parent_category"] for c in b_data["categories"]}
        self.assertEqual(leaf_parents.get("Groceries"), "Food")
        self.assertEqual(leaf_parents.get("Clothing"), "Shopping")

        base_breakdown = self.client.get("/budgets/breakdown?period=month&group_by=base")
        self.assertEqual(base_breakdown.status_code, 200)
        base_cats = {c["category"]: c["total_spend"] for c in base_breakdown.json()["categories"]}
        self.assertEqual(base_cats.get("Food"), 72.97)
        self.assertEqual(base_cats.get("Shopping"), 81.96)

        base_txs = self.client.get("/transactions?category=Shopping&period=month&include_subclasses=true")
        self.assertEqual(base_txs.status_code, 200)
        self.assertEqual(len(base_txs.json()), 1)
        self.assertEqual(base_txs.json()[0]["amount"], -81.96)
        self.assertEqual(base_txs.json()[0]["receipt_id"], res.json()["id"])

        # Edit an individual item's category and tags via PATCH /receipts/{id}/items/{item_id}
        receipt_data = res.json()
        slide_item = next(it for it in receipt_data["items"] if it["item_name"] == "Body Glove Slide")
        patch_res = self.client.patch(
            f"/receipts/{receipt_data['id']}/items/{slide_item['id']}",
            json={"category": "Personal Care", "tags": ["footwear", "summer"]},
        )
        self.assertEqual(patch_res.status_code, 200)
        self.assertEqual(patch_res.json()["category"], "Personal Care")
        self.assertEqual(patch_res.json()["tags"], ["footwear", "summer"])

        updated_breakdown = self.client.get("/budgets/breakdown?period=month")
        updated_cats = {c["category"]: c["total_spend"] for c in updated_breakdown.json()["categories"]}
        self.assertEqual(updated_cats.get("Personal Care"), 26.99)
        self.assertEqual(updated_cats.get("Clothing"), 54.97)

        # Tag inheritance: tagging the main transaction propagates dynamically to all receipt items
        tx_patch = self.client.patch(
            f"/transactions/{tx.id}",
            json={"tags": ["costco-run", "bulk"]},
        )
        self.assertEqual(tx_patch.status_code, 200)

        receipt_detail = self.client.get(f"/receipts/{receipt_data['id']}")
        self.assertEqual(receipt_detail.status_code, 200)
        for item in receipt_detail.json()["items"]:
            self.assertEqual(item["inherited_tags"], ["costco-run", "bulk"])

        # Editing item tags preserves inherited_tags without duplicating parent tags into item.tags
        slide_patch = self.client.patch(
            f"/receipts/{receipt_data['id']}/items/{slide_item['id']}",
            json={"tags": ["footwear", "summer"]},
        )
        self.assertEqual(slide_patch.status_code, 200)
        self.assertEqual(slide_patch.json()["tags"], ["footwear", "summer"])
        self.assertEqual(slide_patch.json()["inherited_tags"], ["costco-run", "bulk"])

        # Tag spending summary expands matched receipts per item and splits by item category
        tag_summary = self.client.get("/transactions/summary/tags?period=month")
        self.assertEqual(tag_summary.status_code, 200)
        tag_map = {item["tag"]: item for item in tag_summary.json()["tags"]}

        costco_tag = tag_map["costco-run"]
        self.assertEqual(costco_tag["total_spend"], 154.93)
        self.assertEqual(costco_tag["transaction_count"], 1)
        costco_cats = {c["category"]: c["total_spend"] for c in costco_tag["categories"]}
        self.assertEqual(costco_cats, {"Groceries": 72.97, "Clothing": 54.97, "Personal Care": 26.99})

        footwear_tag = tag_map["footwear"]
        self.assertEqual(footwear_tag["total_spend"], 26.99)
        self.assertEqual(footwear_tag["transaction_count"], 1)
        self.assertEqual(footwear_tag["categories"][0]["category"], "Personal Care")

        # Filtering transactions by item-only tag returns receipt split carrying inherited + item tags
        footwear_txs = self.client.get("/transactions?tag=footwear&period=month")
        self.assertEqual(footwear_txs.status_code, 200)
        self.assertEqual(len(footwear_txs.json()), 1)
        self.assertEqual(footwear_txs.json()[0]["amount"], -26.99)
        self.assertTrue(footwear_txs.json()[0]["is_receipt_split"])
        self.assertEqual(footwear_txs.json()[0]["tags"], ["costco-run", "bulk", "footwear", "summer"])

        # Filtering transactions by inherited parent tag returns the full transaction
        inherited_txs = self.client.get("/transactions?tag=costco-run&period=month")
        self.assertEqual(inherited_txs.status_code, 200)
        self.assertEqual(len(inherited_txs.json()), 1)
        self.assertEqual(inherited_txs.json()[0]["amount"], -154.93)
        self.assertFalse(inherited_txs.json()[0]["is_receipt_split"])

    def test_rules_apply_to_receipt_items_and_fallback_to_jev_with_existing_tags(self):
        # Seed existing tags in ledger so Jev & heuristic reuse them
        seeded_tx = Transaction(
            transaction_date=date.today(),
            account_id=self.acc.id,
            amount=-20.00,
            currency="NZD",
            merchant="Local Cafe",
            category="Food",
            tags='["coffee-beans", "badminton"]',
            raw_hash="hash_existing_tags_seed",
        )
        self.db.add(seeded_tx)
        self.db.commit()

        # 1. Create a rule matching 'CeraVe' -> Personal Care + skincare tags
        rule_res = self.client.post(
            "/rules",
            json={
                "name": "CeraVe Skincare",
                "description_pattern": "cerave",
                "match_type": "contains",
                "target_category": "Personal Care",
                "target_tags": ["skincare", "cleanser"],
                "priority": 5,
                "is_active": True,
                "apply_retroactively": False,
            },
        )
        self.assertEqual(rule_res.status_code, 201)

        # 2. Create a receipt with one rule-matching item and one uncategorized item
        from app.models import Category

        self.db.add(Category(name="Sports Gear", is_active=True, is_system=False, sort_order=150))
        self.db.commit()

        seen_jev_calls = []

        def fake_jev(merchant, amount, notes=None, existing_tags=None, extra_categories=None):
            seen_jev_calls.append(
                {
                    "merchant": merchant,
                    "existing_tags": existing_tags,
                    "extra_categories": extra_categories,
                }
            )
            return ("Leisure", False, ["badminton"])

        with patch("app.services.receipt_service.classify_transaction_jev_with_tags", side_effect=fake_jev):
            create_res = self.client.post(
                "/receipts",
                json={
                    "receipt_date": date.today().isoformat(),
                    "merchant": "Chemist & Sport",
                    "total_amount": 64.90,
                    "items": [
                        {
                            "item_name": "CeraVe Hydrating Wash 236ml",
                            "quantity": 1.0,
                            "unit_price": 24.90,
                            "total_price": 24.90,
                        },
                        {
                            "item_name": "Yonex Aerosensa Feather Pack",
                            "quantity": 1.0,
                            "unit_price": 40.00,
                            "total_price": 40.00,
                        },
                    ],
                },
            )

        self.assertEqual(create_res.status_code, 201)
        items_by_name = {it["item_name"]: it for it in create_res.json()["items"]}

        # Rule-matched item takes rule category & tags and skips Jev
        cerave = items_by_name["CeraVe Hydrating Wash 236ml"]
        self.assertEqual(cerave["category"], "Personal Care")
        self.assertEqual(cerave["tags"], ["skincare", "cleanser"])

        # Unmatched item falls back to Jev, receiving existing ledger tags as context
        yonex = items_by_name["Yonex Aerosensa Feather Pack"]
        self.assertEqual(yonex["category"], "Leisure")
        self.assertIn("badminton", yonex["tags"])
        self.assertEqual(len(seen_jev_calls), 1)
        self.assertEqual(seen_jev_calls[0]["merchant"], "Yonex Aerosensa Feather Pack")
        self.assertIn("badminton", seen_jev_calls[0]["existing_tags"])
        self.assertIn("coffee-beans", seen_jev_calls[0]["existing_tags"])
        self.assertEqual(seen_jev_calls[0]["extra_categories"], ["Sports Gear"])

        # 3. Creating a new rule later with apply_retroactively=True updates existing receipt items
        yonex_rule = self.client.post(
            "/rules",
            json={
                "name": "Yonex Gear",
                "description_pattern": "yonex",
                "match_type": "contains",
                "target_category": "Health",
                "target_tags": ["sport-gear"],
                "priority": 10,
                "is_active": True,
                "apply_retroactively": True,
            },
        )
        self.assertEqual(yonex_rule.status_code, 201)
        self.assertEqual(yonex_rule.json()["match_count"], 1)

        refreshed_receipt = self.client.get(f"/receipts/{create_res.json()['id']}")
        self.assertEqual(refreshed_receipt.status_code, 200)
        refreshed_yonex = next(
            it for it in refreshed_receipt.json()["items"] if "Yonex" in it["item_name"]
        )
        self.assertEqual(refreshed_yonex["category"], "Health")
        self.assertEqual(refreshed_yonex["tags"], ["badminton", "sport-gear"])

    def test_extract_tags_omits_general_fallback_tag(self):
        self.assertEqual(extract_tags_for_line_item("Mystery Item"), [])
        self.assertEqual(extract_tags_for_line_item("Mystery Item", domain="general"), [])
        self.assertEqual(
            extract_tags_for_line_item(
                "Copper & Oak Pant", domain="general", product_type="clothing"
            ),
            ["clothing"],
        )

        with patch(
            "app.services.receipt_service.classify_transaction_jev_with_tags",
            return_value=("General", False, []),
        ):
            res = self.client.post(
                "/receipts",
                json={
                    "receipt_date": date.today().isoformat(),
                    "merchant": "Corner Shop",
                    "total_amount": 12.00,
                    "items": [
                        {
                            "item_name": "Zebra Gizmo",
                            "quantity": 1.0,
                            "unit_price": 12.00,
                            "total_price": 12.00,
                        }
                    ],
                },
            )
        self.assertEqual(res.status_code, 201)
        item = res.json()["items"][0]
        self.assertEqual(item["tags"], [])
        products_res = self.client.get("/products")
        self.assertEqual(products_res.status_code, 200)
        self.assertEqual(products_res.json()["products"][0]["tags"], [])
