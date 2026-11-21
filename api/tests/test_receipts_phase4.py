"""Phase 4 tests for multi-order receipts, HTML sanitization, FX matching, candidates, and /integrations/email."""

import base64
from datetime import date, timedelta
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.models import Account, ExchangeRate, IngestedEmail, Receipt, Transaction
from app.services.receipt_service import (
    parse_receipt_orders_text_with_llm,
    parse_receipt_text_with_llm,
    parse_receipt_with_llm,
    reconcile_unlinked_receipts,
    sanitize_receipt_email_content,
)

TEST_DB_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _make_png_bytes(width: int = 800, height: int = 800, *, large: bool = True) -> bytes:
    img = Image.new("RGB", (width, height), color=(240, 240, 240))
    if large:
        draw = ImageDraw.Draw(img)
        for y in range(20, height - 20, 12):
            draw.line([(20, y), (width - 20, y)], fill=(y % 200, (y * 3) % 200, (y * 7) % 200), width=4)
            draw.rectangle([40, y, width - 40, y + 6], outline=(10, 40, 90))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    raw = buf.getvalue()
    if large and len(raw) < 16384:
        # Pad PNG safely using a valid tEXt-style or raw trailing padding only if needed;
        # deterministic noise makes PNG naturally > 20KB without invalidating image format.
        import random

        rng = random.Random(42)
        px = img.load()
        for x in range(width):
            for y in range(0, height, 2):
                px[x, y] = (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
        buf2 = io.BytesIO()
        img.save(buf2, format="PNG")
        return buf2.getvalue()
    return raw


class TestReceiptsPhase4(unittest.TestCase):
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

        self._orig_webhook = settings.webhook_secret
        self._orig_env = settings.environment
        self._orig_llm_key = settings.llm_api_key

        self.acc_wise_nzd = Account(
            name="Wise NZD",
            account_type="checking",
            tier="cash",
            institution="Wise",
            currency="NZD",
        )
        self.acc_asb = Account(
            name="ASB Checking",
            account_type="checking",
            tier="cash",
            institution="ASB",
            currency="NZD",
        )
        self.acc_dbs = Account(
            name="DBS SGD",
            account_type="savings",
            tier="cash",
            institution="DBS Bank",
            currency="SGD",
        )
        self.db.add_all([self.acc_wise_nzd, self.acc_asb, self.acc_dbs])
        self.db.commit()
        for a in (self.acc_wise_nzd, self.acc_asb, self.acc_dbs):
            self.db.refresh(a)

    def tearDown(self):
        settings.webhook_secret = self._orig_webhook
        settings.environment = self._orig_env
        settings.llm_api_key = self._orig_llm_key
        app.dependency_overrides.pop(get_db, None)
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_html_sanitizer_preserves_aria_label_and_img_alt_while_stripping_bloat(self):
        html = (
            "<html><head><style>.hide{display:none}</style></head><body>"
            "<p>---------- Forwarded message ---------<br>"
            "From: Amazon AU &lt;auto-confirm@amazon.com.au&gt;<br>"
            "Date: Sat, Nov 7, 2026 at 10:15 AM<br>"
            "Subject: Your Amazon.com.au order</p>"
            "<img src='https://tracker.example.com/pixel.gif' alt='pixel' />"
            "<a href='https://tracker.example.com/gp/r.html?C=1234567890&K=ABCDEFGHIJKLMNOPQRSTUVWXYZ'>"
            "<img alt='Anker Prime 100W USB-C GaN Charger 3-Port' src='cid:1' />"
            "<span>Anker Prime...</span></a>"
            "<span aria-label='AUD 179.74'><span>$</span><span>179</span><span>.</span><span>74</span></span>"
            "<p>Track at https://tracker.example.com/gp/r.html?C=1234567890&K=ABCDEFGHIJKLMNOPQRSTUVWXYZ12345</p>"
            "<script>var x=1;</script></body></html>"
        )
        cleaned, fb_date = sanitize_receipt_email_content(
            subject="Fwd: Your Amazon.com.au order",
            from_address="alex@example.com",
            received_at=None,
            text_body=None,
            html_excerpt=html,
            raw_excerpt=None,
        )
        self.assertEqual(fb_date, date(2026, 11, 7))
        self.assertIn("AUD 179.74", cleaned)
        self.assertIn("Anker Prime 100W USB-C GaN Charger 3-Port", cleaned)
        self.assertNotIn("display:none", cleaned)
        self.assertNotIn("var x=1", cleaned)
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ", cleaned)

    def test_multi_order_email_receipt_and_delayed_wise_original_fx_matching(self):
        # Delayed shipment Wise card charges (+14d and +15d) in NZD with raw_description in AUD.
        # Second charge tests tolerance 135.99 vs 135.98 AUD.
        tx1 = Transaction(
            transaction_date=date(2026, 11, 15),
            account_id=self.acc_wise_nzd.id,
            amount=-196.40,
            currency="NZD",
            merchant="Amazon AU",
            category="Shopping",
            raw_description="Card transaction of 179.74 AUD issued by Amazon Marketplace AU",
            raw_hash="wise-amz-1",
        )
        tx2 = Transaction(
            transaction_date=date(2026, 11, 16),
            account_id=self.acc_wise_nzd.id,
            amount=-148.20,
            currency="NZD",
            merchant="Amazon AU",
            category="Shopping",
            raw_description="Card transaction of 135.98 AUD issued by Amazon AU",
            raw_hash="wise-amz-2",
        )
        # Positive refund / inflow of same amount must NEVER match
        tx_refund = Transaction(
            transaction_date=date(2026, 11, 15),
            account_id=self.acc_wise_nzd.id,
            amount=196.40,
            currency="NZD",
            merchant="Amazon AU Refund",
            category="Shopping",
            raw_description="Card transaction of 179.74 AUD issued by Amazon Marketplace AU",
            raw_hash="wise-amz-refund",
        )
        self.db.add_all([tx1, tx2, tx_refund])
        self.db.commit()
        self.db.refresh(tx1)
        self.db.refresh(tx2)

        # Small inline logo (<15KB) must be ignored in favor of HTML/text parsing
        tiny_logo = _make_png_bytes(64, 64, large=False)
        self.assertLess(len(tiny_logo), 15 * 1024)

        mock_multi_orders = {
            "orders": [
                {
                    "order_number": "503-1111111-2222222",
                    "merchant": "Amazon AU",
                    "receipt_date": "2026-11-01",
                    "currency": "AUD",
                    "total_amount": 179.74,
                    "items": [
                        {
                            "item_name": "Anker Prime 100W USB-C Charger",
                            "quantity": 1.0,
                            "unit_price": 179.74,
                            "total_price": 179.74,
                            "category": "Tech",
                        }
                    ],
                },
                {
                    "order_number": "503-3333333-4444444",
                    "merchant": "Amazon AU",
                    "receipt_date": "2026-11-01",
                    "currency": "AUD",
                    "total_amount": 135.99,
                    "items": [
                        {
                            "item_name": "Optimum Nutrition Whey 1kg",
                            "quantity": 1.0,
                            "unit_price": 135.99,
                            "total_price": 135.99,
                            "category": "Supplements",
                        }
                    ],
                },
            ]
        }

        with patch(
            "app.services.receipt_service._invoke_receipt_llm",
            return_value=mock_multi_orders,
        ):
            resp = self.client.post(
                "/integrations/email",
                json={
                    "email_id": 201,
                    "message_id": "<amz-multi-1@example.com>",
                    "route_target": "plutus_purchase_receipt",
                    "subject": "Your Amazon.com.au orders",
                    "received_at": "2026-11-01T02:00:00Z",
                    "html_excerpt": "<div>Order 503-1111111-2222222 AUD 179.74</div><div>Order 503-3333333-4444444 AUD 135.99</div>",
                    "attachments": [
                        {
                            "filename": "logo.png",
                            "content_type": "image/png",
                            "size_bytes": len(tiny_logo),
                            "content_base64": base64.b64encode(tiny_logo).decode("ascii"),
                        }
                    ],
                },
            )

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "processed")
        self.assertEqual(len(data["receipt_ids"]), 2)
        self.assertEqual(data["consumer_ref"], f"receipts:{data['receipt_ids'][0]},{data['receipt_ids'][1]}")

        r1 = self.db.get(Receipt, data["receipt_ids"][0])
        r2 = self.db.get(Receipt, data["receipt_ids"][1])
        self.assertEqual(r1.currency, "AUD")
        self.assertEqual(r1.order_number, "503-1111111-2222222")
        self.assertEqual(r1.status, "matched")
        self.assertEqual(r1.transaction_id, tx1.id)
        self.assertFalse(r1.has_image)

        self.assertEqual(r2.currency, "AUD")
        self.assertEqual(r2.order_number, "503-3333333-4444444")
        self.assertEqual(r2.status, "matched")
        self.assertEqual(r2.transaction_id, tx2.id)

        # Replay with a DIFFERENT message_id triggers semantic dedup instead of creating duplicates
        with patch(
            "app.services.receipt_service._invoke_receipt_llm",
            return_value=mock_multi_orders,
        ):
            dup_semantic = self.client.post(
                "/integrations/email",
                json={
                    "email_id": 202,
                    "message_id": "<amz-multi-1-re-forward@example.com>",
                    "route_target": "plutus_purchase_receipt",
                    "subject": "Fwd: Your Amazon.com.au orders",
                    "text_body": "Order 503-1111111-2222222 and 503-3333333-4444444",
                },
            )
        self.assertEqual(dup_semantic.status_code, 200)
        self.assertEqual(dup_semantic.json()["receipt_ids"], data["receipt_ids"])
        self.assertEqual(len(self.db.scalars(select(Receipt)).all()), 2)

    def test_valid_large_attachment_first_and_currency_preservation_on_upload(self):
        large_png = _make_png_bytes(256, 256, large=True)
        self.assertGreaterEqual(len(large_png), 15 * 1024)

        mock_vision = {
            "merchant": "Mecca Cosmetica",
            "receipt_date": "2026-11-18",
            "currency": "AUD",
            "order_number": "MEC-7788",
            "total_amount": 85.00,
            "items": [
                {
                    "item_name": "Niacinamide Serum 30ml",
                    "quantity": 1.0,
                    "unit_price": 85.00,
                    "total_price": 85.00,
                }
            ],
        }
        with patch("app.services.receipt_service.parse_receipt_with_llm", return_value=mock_vision) as mock_vis:
            resp = self.client.post(
                "/integrations/email",
                json={
                    "email_id": 203,
                    "message_id": "<att-large-1@example.com>",
                    "route_target": "plutus_purchase_receipt",
                    "subject": "Your Mecca receipt",
                    "text_body": "Thank you for your purchase.",
                    "attachments": [
                        {
                            "filename": "mecca_receipt.png",
                            "content_type": "image/png",
                            "size_bytes": len(large_png),
                            "content_base64": base64.b64encode(large_png).decode("ascii"),
                        }
                    ],
                },
            )
            mock_vis.assert_called_once()

        self.assertEqual(resp.status_code, 200)
        rec = self.db.get(Receipt, resp.json()["receipt_id"])
        self.assertTrue(rec.has_image)
        self.assertEqual(rec.currency, "AUD")
        self.assertEqual(rec.order_number, "MEC-7788")
        self.assertEqual(rec.image_filename, "mecca_receipt.png")

        # Direct /receipts/upload must ALSO preserve parsed currency (no NZD clobber bug)
        with patch("app.routers.receipts.parse_receipt_with_llm", return_value=mock_vision):
            up = self.client.post(
                "/receipts/upload",
                files={"file": ("direct.png", large_png, "image/png")},
            )
        self.assertEqual(up.status_code, 201)
        self.assertEqual(up.json()["currency"], "AUD")
        self.assertEqual(up.json()["order_number"], "MEC-7788")

    def test_strict_currency_guard_and_cross_currency_needs_review_candidates_and_link_conflict(self):
        # 1. SGD 50.00 receipt must NOT falsely auto-match an NZD 50.00 transaction
        tx_nzd_50 = Transaction(
            transaction_date=date(2026, 11, 10),
            account_id=self.acc_asb.id,
            amount=-50.00,
            currency="NZD",
            merchant="Random NZD Store",
            category="Shopping",
            raw_hash="nzd-50-unrelated",
        )
        self.db.add(tx_nzd_50)
        self.db.commit()

        sgd_res = self.client.post(
            "/receipts",
            json={
                "receipt_date": "2026-11-10",
                "merchant": "FairPrice Finest",
                "total_amount": 50.00,
                "currency": "SGD",
                "items": [],
            },
        )
        self.assertEqual(sgd_res.status_code, 201)
        self.assertEqual(sgd_res.json()["status"], "unlinked")
        self.assertIsNone(sgd_res.json()["transaction_id"])

        # 2. Cross-currency FX candidate (AUD 100.00 receipt vs NZD -109.00 ASB tx with cached FX 0.918 -> 100.06 AUD)
        tx_date = date(2026, 11, 12)
        self.db.add(
            ExchangeRate(
                rate_date=tx_date,
                from_currency="NZD",
                to_currency="AUD",
                rate=0.918,
                source="test",
            )
        )
        tx_asb_aud = Transaction(
            transaction_date=tx_date,
            account_id=self.acc_asb.id,
            amount=-109.00,
            currency="NZD",
            merchant="CHEMIST WAREHOUSE AU",
            category="Health",
            raw_description="CHEMIST WAREHOUSE AU SYDNEY",
            raw_hash="asb-aud-109",
        )
        self.db.add(tx_asb_aud)
        self.db.commit()
        self.db.refresh(tx_asb_aud)

        aud_res = self.client.post(
            "/receipts",
            json={
                "receipt_date": "2026-11-12",
                "merchant": "Chemist Warehouse",
                "total_amount": 100.00,
                "currency": "AUD",
                "items": [],
            },
        )
        self.assertEqual(aud_res.status_code, 201)
        aud_receipt = aud_res.json()
        self.assertEqual(aud_receipt["status"], "needs_review")
        self.assertIsNone(aud_receipt["transaction_id"])

        # Fetch candidates endpoint
        cand_res = self.client.get(f"/receipts/{aud_receipt['id']}/candidates")
        self.assertEqual(cand_res.status_code, 200)
        cands = cand_res.json()
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["transaction_id"], tx_asb_aud.id)
        self.assertEqual(cands[0]["account_name"], "ASB Checking")
        self.assertEqual(cands[0]["match_reason"], "fx_estimated")
        self.assertAlmostEqual(cands[0]["estimated_receipt_amount"], 100.06, places=2)
        self.assertGreater(cands[0]["confidence"], 0.7)

        # Manual 1-tap link sets matched
        link_res = self.client.post(f"/receipts/{aud_receipt['id']}/link/{tx_asb_aud.id}")
        self.assertEqual(link_res.status_code, 200)
        self.assertEqual(link_res.json()["status"], "matched")
        self.assertEqual(link_res.json()["transaction_id"], tx_asb_aud.id)

        # Linking a second receipt to the same transaction must return 409 Conflict
        other_res = self.client.post(
            "/receipts",
            json={
                "receipt_date": "2026-11-12",
                "merchant": "Another Store",
                "total_amount": 100.00,
                "currency": "AUD",
                "items": [],
            },
        )
        self.assertEqual(other_res.status_code, 201)
        conflict_res = self.client.post(f"/receipts/{other_res.json()['id']}/link/{tx_asb_aud.id}")
        self.assertEqual(conflict_res.status_code, 409)

    def test_ambiguous_same_currency_and_reconcile_picks_up_needs_review(self):
        d = date(2026, 11, 14)
        t1 = Transaction(
            transaction_date=d,
            account_id=self.acc_asb.id,
            amount=-6.50,
            currency="NZD",
            merchant="Mojo Coffee",
            category="Dining",
            raw_hash="mojo-1",
        )
        t2 = Transaction(
            transaction_date=d,
            account_id=self.acc_asb.id,
            amount=-6.50,
            currency="NZD",
            merchant="Mojo Coffee",
            category="Dining",
            raw_hash="mojo-2",
        )
        self.db.add_all([t1, t2])
        self.db.commit()

        rec_res = self.client.post(
            "/receipts",
            json={
                "receipt_date": d.isoformat(),
                "merchant": "Mojo Coffee",
                "total_amount": 6.50,
                "currency": "NZD",
                "items": [],
            },
        )
        self.assertEqual(rec_res.status_code, 201)
        self.assertEqual(rec_res.json()["status"], "needs_review")
        cands = self.client.get(f"/receipts/{rec_res.json()['id']}/candidates").json()
        self.assertEqual(len(cands), 2)

        # If a receipt was staged in needs_review from FX and an exact matching tx arrives later,
        # reconcile_unlinked_receipts matches it cleanly.
        r_fx = Receipt(
            receipt_date=date(2026, 11, 20),
            merchant="Kmart",
            total_amount=33.00,
            currency="NZD",
            status="needs_review",
        )
        self.db.add(r_fx)
        self.db.add(
            Transaction(
                transaction_date=date(2026, 11, 20),
                account_id=self.acc_asb.id,
                amount=-33.00,
                currency="NZD",
                merchant="Kmart Albany",
                category="Shopping",
                raw_hash="kmart-exact-33",
            )
        )
        self.db.commit()
        matched = reconcile_unlinked_receipts(self.db)
        self.assertEqual(matched, 1)
        self.db.refresh(r_fx)
        self.assertEqual(r_fx.status, "matched")

    def test_integrations_email_auth_statement_staging_and_retry_after_failed(self):
        settings.environment = "production"
        settings.webhook_secret = "hook-secret-xyz"
        settings.llm_api_key = ""

        unauth = self.client.post(
            "/integrations/email",
            json={
                "message_id": "<unauth@example.com>",
                "route_target": "plutus_bank_statement",
                "subject": "Statement",
            },
        )
        self.assertEqual(unauth.status_code, 401)

        headers = {"Authorization": "Bearer hook-secret-xyz"}

        # 1. Statement route stages as needs_review
        stmt_res = self.client.post(
            "/integrations/email",
            headers=headers,
            json={
                "email_id": 301,
                "message_id": "<stmt-1@example.com>",
                "route_target": "plutus_bank_statement",
                "subject": "Monthly Statement",
            },
        )
        self.assertEqual(stmt_res.status_code, 200)
        self.assertEqual(stmt_res.json()["status"], "needs_review")
        self.assertEqual(stmt_res.json()["consumer_ref"], "staged:statement")

        # 2. Failed parse persists status='failed', and retry with same message_id succeeds cleanly
        fail_res = self.client.post(
            "/integrations/email",
            headers=headers,
            json={
                "email_id": 302,
                "message_id": "<retry-after-fail@example.com>",
                "route_target": "plutus_purchase_receipt",
                "subject": "Empty receipt",
                "text_body": "",
                "html_excerpt": "",
            },
        )
        self.assertEqual(fail_res.status_code, 422)
        failed_audit = self.db.scalar(
            select(IngestedEmail).where(IngestedEmail.message_id == "<retry-after-fail@example.com>")
        )
        self.assertIsNotNone(failed_audit)
        self.assertEqual(failed_audit.status, "failed")

        retry_order = {
            "orders": [
                {
                    "order_number": "RETRY-100",
                    "merchant": "Apple Store",
                    "receipt_date": "2026-11-19",
                    "currency": "NZD",
                    "total_amount": 49.00,
                    "items": [],
                }
            ]
        }
        with patch("app.services.receipt_service._invoke_receipt_llm", return_value=retry_order):
            retry_res = self.client.post(
                "/integrations/email",
                headers=headers,
                json={
                    "email_id": 302,
                    "message_id": "<retry-after-fail@example.com>",
                    "route_target": "plutus_purchase_receipt",
                    "subject": "Apple Receipt",
                    "text_body": "Order RETRY-100 NZD 49.00",
                },
            )
        self.assertEqual(retry_res.status_code, 200)
        self.assertEqual(retry_res.json()["status"], "processed")
        self.assertFalse(retry_res.json()["duplicate"])
        self.db.refresh(failed_audit)
        self.assertEqual(failed_audit.status, "processed")
        self.assertEqual(failed_audit.consumer_ref, f"receipt:{retry_res.json()['receipt_id']}")

    def test_text_receipt_llm_routes_to_fast_text_model_with_json_mode_and_60s_timeout(self):
        settings.llm_api_key = "test-llm-key"
        recorded: list[tuple[dict, int]] = []

        def fake_urlopen(req, timeout=None):
            recorded.append((json.loads(req.data.decode("utf-8")), timeout))
            return _FakeLlmResponse(
                {"orders": [{"merchant": "Demo Store", "total_amount": 12.5}]}
            )

        with patch("app.services.receipt_service.urllib.request.urlopen", side_effect=fake_urlopen):
            orders_res = parse_receipt_orders_text_with_llm("Order SYN-101 Total NZD 12.50")
            text_res = parse_receipt_text_with_llm("Receipt Demo Store Total NZD 12.50")

        self.assertIsNotNone(orders_res)
        self.assertIsNotNone(text_res)
        self.assertEqual(len(recorded), 2)
        for payload, timeout in recorded:
            self.assertEqual(payload["model"], settings.resolved_llm_model)
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            self.assertEqual(timeout, 60)

    def test_image_receipt_llm_routes_to_vision_model_with_105s_timeout_without_json_mode(self):
        settings.llm_api_key = "test-llm-key"
        recorded: list[tuple[dict, int]] = []
        png_bytes = _make_png_bytes(128, 128, large=False)

        def fake_urlopen(req, timeout=None):
            recorded.append((json.loads(req.data.decode("utf-8")), timeout))
            return _FakeLlmResponse({"merchant": "Demo Mart", "total_amount": 25.0, "items": []})

        with patch("app.services.receipt_service.urllib.request.urlopen", side_effect=fake_urlopen):
            res = parse_receipt_with_llm(png_bytes, "receipt.png", "image/png")

        self.assertIsNotNone(res)
        self.assertEqual(len(recorded), 1)
        payload, timeout = recorded[0]
        self.assertEqual(payload["model"], settings.resolved_receipt_llm_model)
        self.assertNotIn("response_format", payload)
        self.assertEqual(timeout, 105)


class _FakeLlmResponse:
    """Minimal context-manager mock for urllib.request.urlopen responses."""

    def __init__(self, content_obj: dict):
        self._body = json.dumps(
            {"choices": [{"message": {"content": json.dumps(content_obj)}}]}
        ).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeLlmResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


if __name__ == "__main__":
    unittest.main()
