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
from app.schemas import EmailAttachmentPayload, InboundEmailDispatchPayload
from app.services.ai_gateway import (
    AiConfigError,
    AiInvalidResponseError,
    AiUpstreamError,
    InMemoryAiGateway,
    get_ai_gateway,
)
from app.services.connectors import ConnectorUpstreamError
from app.services.receipt_service import (
    ingest_purchase_receipt_email,
    match_receipt_to_transaction,
    normalize_receipt_currency,
    parse_original_fx_from_description,
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
        app.dependency_overrides.pop(get_ai_gateway, None)
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

        gateway = InMemoryAiGateway(
            text_responses=[mock_multi_orders, mock_multi_orders],
            jev_handler=lambda _req: '{"category": "General", "is_anomaly": false, "primary_tag": "none"}',
        )
        app.dependency_overrides[get_ai_gateway] = lambda: gateway

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
        gateway = InMemoryAiGateway(
            vision_responses=[mock_vision, mock_vision],
            jev_handler=lambda _req: '{"category": "Personal Care", "is_anomaly": false, "primary_tag": "none"}',
        )
        app.dependency_overrides[get_ai_gateway] = lambda: gateway

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
        self.assertEqual(len(gateway.vision_requests), 1)

        self.assertEqual(resp.status_code, 200)
        rec = self.db.get(Receipt, resp.json()["receipt_id"])
        self.assertTrue(rec.has_image)
        self.assertEqual(rec.currency, "AUD")
        self.assertEqual(rec.order_number, "MEC-7788")
        self.assertEqual(rec.image_filename, "mecca_receipt.png")

        # Direct /receipts/upload must ALSO preserve parsed currency (no NZD clobber bug)
        up = self.client.post(
            "/receipts/upload",
            files={"file": ("direct.png", large_png, "image/png")},
        )
        self.assertEqual(up.status_code, 201)
        self.assertEqual(up.json()["currency"], "AUD")
        self.assertEqual(up.json()["order_number"], "MEC-7788")
        self.assertEqual(len(gateway.vision_requests), 2)

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
        gateway = InMemoryAiGateway(text_responses=[retry_order])
        app.dependency_overrides[get_ai_gateway] = lambda: gateway
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
        gateway = InMemoryAiGateway(
            text_responses=[
                {"orders": [{"merchant": "Demo Store", "total_amount": 12.5}]},
                {"merchant": "Demo Store", "total_amount": 12.5},
            ]
        )
        orders_res = parse_receipt_orders_text_with_llm(
            "Order SYN-101 Total NZD 12.50", gateway=gateway
        )
        text_res = parse_receipt_text_with_llm(
            "Receipt Demo Store Total NZD 12.50", gateway=gateway
        )

        self.assertIsNotNone(orders_res)
        self.assertIsNotNone(text_res)
        self.assertEqual(len(gateway.text_requests), 2)
        for req in gateway.text_requests:
            self.assertEqual(req.model, settings.resolved_llm_model)
            self.assertTrue(req.response_format_json)
            self.assertEqual(req.timeout, 60.0)

    def test_image_receipt_llm_routes_to_vision_model_with_105s_timeout_without_json_mode(self):
        png_bytes = _make_png_bytes(128, 128, large=False)
        gateway = InMemoryAiGateway(
            vision_responses=[{"merchant": "Demo Mart", "total_amount": 25.0, "items": []}]
        )
        res = parse_receipt_with_llm(png_bytes, "receipt.png", "image/png", gateway=gateway)

        self.assertIsNotNone(res)
        self.assertEqual(len(gateway.vision_requests), 1)
        req = gateway.vision_requests[0]
        self.assertEqual(req.model, settings.resolved_receipt_llm_model)
        self.assertFalse(req.response_format_json)
        self.assertEqual(req.timeout, 105.0)

    def test_receipt_llm_error_mapping_respects_raise_on_error(self):
        gateway = InMemoryAiGateway(
            text_responses=[
                AiConfigError("Missing API key"),
                AiInvalidResponseError("Malformed JSON"),
                AiUpstreamError("Gateway timeout", status_code=503),
                AiUpstreamError("Gateway timeout", status_code=503),
            ]
        )
        with self.assertRaises(ValueError):
            parse_receipt_orders_text_with_llm("Order 1", raise_on_error=True, gateway=gateway)
        with self.assertRaises(ValueError):
            parse_receipt_orders_text_with_llm("Order 2", raise_on_error=True, gateway=gateway)
        with self.assertRaises(ConnectorUpstreamError):
            parse_receipt_orders_text_with_llm("Order 3", raise_on_error=True, gateway=gateway)
        self.assertIsNone(
            parse_receipt_orders_text_with_llm("Order 4", raise_on_error=False, gateway=gateway)
        )

    def test_parse_original_fx_from_description_wise_and_nz_bank_formats(self):
        cases = [
            ("Card transaction of 11,700.00 PHP issued by Harbour Villa", (11700.00, "PHP")),
            ("VT JETSTAR AIR (PHP 41194.11 @ 34.98 )", (41194.11, "PHP")),
            ("MERCHANT ABC (SGD 336.20 @ 0.7131)", (336.20, "SGD")),
            ("MERCHANT DEF (PHP 4,533.80 @ 35.0343)", (4533.80, "PHP")),
            ("DEMO STORE AUD 16.29 converted at 0.8281", (16.29, "AUD")),
            ("AUD 202.80 converted at 0.81", (202.80, "AUD")),
            ("MAY 12 @ STORE (PHP 4,533.80 @ 35.0343)", (4533.80, "PHP")),
        ]
        for desc, expected in cases:
            self.assertEqual(parse_original_fx_from_description(desc), expected, msg=desc)

        non_fx_cases = [
            None,
            "",
            "Countdown Albany Purchase",
            "Transfer to ANZ 1234",
            "Card payment of 12.00 OCT",
            "OCT 12.00 @ STORE",
            "USD 0.00 @ 0.60",
        ]
        for desc in non_fx_cases:
            self.assertIsNone(parse_original_fx_from_description(desc), msg=desc)

    def test_match_receipt_to_transaction_nz_bank_fx_jetstar_auto_match(self):
        tx = Transaction(
            transaction_date=date(2026, 10, 12),
            account_id=self.acc_asb.id,
            amount=-1192.66,
            currency="NZD",
            merchant="VT JETSTAR AIR",
            category="Travel",
            raw_description="VT JETSTAR AIR (PHP 41194.11 @ 34.98 )",
            raw_hash="nz-bank-fx-jetstar-1",
        )
        self.db.add(tx)
        self.db.commit()
        self.db.refresh(tx)

        receipt = Receipt(
            receipt_date=date(2026, 10, 8),
            merchant="Jetstar Airways Pty. Ltd.",
            total_amount=41194.11,
            currency="PHP",
            status="unlinked",
            is_processed=True,
        )
        self.db.add(receipt)
        self.db.commit()
        self.db.refresh(receipt)

        matched_tx = match_receipt_to_transaction(receipt, self.db)
        self.assertIsNotNone(matched_tx)
        self.assertEqual(matched_tx.id, tx.id)
        self.assertEqual(receipt.status, "matched")
        self.assertEqual(receipt.transaction_id, tx.id)

    def test_normalize_receipt_currency_unicode_symbols(self):
        cases = [
            ("₱", "PHP"),
            ("PH₱", "PHP"),
            ("PHP", "PHP"),
            ("₱11,700.00", "PHP"),
            ("€", "EUR"),
            ("£", "GBP"),
            ("S$", "SGD"),
            ("SG$", "SGD"),
            ("NZ$", "NZD"),
            ("AU$", "AUD"),
            ("A$", "AUD"),
            ("US$", "USD"),
            (None, "NZD"),
        ]
        for raw, expected in cases:
            self.assertEqual(normalize_receipt_currency(raw), expected, msg=raw)

    def test_ingest_purchase_receipt_email_retains_image_on_vision_fallback(self):
        large_png = _make_png_bytes(256, 256, large=True)
        self.assertGreaterEqual(len(large_png), 15 * 1024)
        fallback_orders = {
            "orders": [
                {
                    "order_number": "INV-9001",
                    "merchant": "Harbour Villa Hotel",
                    "receipt_date": "2026-11-20",
                    "currency": "PH₱",
                    "total_amount": 11700.00,
                    "items": [
                        {
                            "item_name": "Room Downpayment",
                            "quantity": 1.0,
                            "unit_price": 11700.00,
                            "total_price": 11700.00,
                            "category": "Travel",
                        }
                    ],
                }
            ]
        }
        img_payload = InboundEmailDispatchPayload(
            email_id=401,
            message_id="<fallback-img-1@example.com>",
            route_target="plutus_purchase_receipt",
            subject="Booking Deposit Receipt",
            from_address="reservations@example.com",
            text_body="Thank you Alex Tan. Downpayment Php 11,700.00 received for Harbour Villa Hotel.",
            attachments=[
                EmailAttachmentPayload(
                    filename="deposit_receipt.png",
                    content_type="image/png",
                    size_bytes=len(large_png),
                    content_base64=base64.b64encode(large_png).decode("ascii"),
                )
            ],
        )

        gateway = InMemoryAiGateway(
            vision_responses=[AiUpstreamError("Vision unavailable", status_code=503)],
            text_responses=[fallback_orders],
            jev_handler=lambda _req: '{"category": "Travel", "is_anomaly": false, "primary_tag": "none"}',
        )
        created = ingest_purchase_receipt_email(self.db, img_payload, gateway=gateway)
        self.assertEqual(len(gateway.vision_requests), 1)
        self.assertEqual(len(gateway.text_requests), 1)

        self.assertEqual(len(created), 1)
        rec = created[0]
        self.assertIsNotNone(rec.image_data)
        self.assertTrue(rec.has_image)
        self.assertEqual(rec.image_mime_type, "image/webp")
        self.assertEqual(rec.image_filename, "deposit_receipt.png")
        self.assertEqual(rec.currency, "PHP")
        self.assertEqual(rec.total_amount, 11700.00)

    def test_ingest_purchase_receipt_email_retains_pdf_on_vision_fallback(self):
        raw_pdf = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n" + (b"0" * 16000)
        pdf_fallback_orders = {
            "orders": [
                {
                    "order_number": "INV-9002",
                    "merchant": "Pacific Airlines",
                    "receipt_date": "2026-11-20",
                    "currency": "€",
                    "total_amount": 320.50,
                    "items": [],
                }
            ]
        }
        pdf_payload = InboundEmailDispatchPayload(
            email_id=402,
            message_id="<fallback-pdf-1@example.com>",
            route_target="plutus_purchase_receipt",
            subject="Flight Itinerary",
            from_address="bookings@example.com",
            text_body="Flight confirmation INV-9002 total EUR 320.50 for Alex Tan.",
            attachments=[
                EmailAttachmentPayload(
                    filename="itinerary.pdf",
                    content_type="application/pdf",
                    size_bytes=len(raw_pdf),
                    content_base64=base64.b64encode(raw_pdf).decode("ascii"),
                )
            ],
        )

        gateway = InMemoryAiGateway(text_responses=[pdf_fallback_orders])
        with patch("app.services.receipt_service.extract_pdf_text", return_value=""):
            pdf_created = ingest_purchase_receipt_email(self.db, pdf_payload, gateway=gateway)

        self.assertEqual(len(gateway.text_requests), 1)
        self.assertEqual(len(pdf_created), 1)
        pdf_rec = pdf_created[0]
        self.assertEqual(pdf_rec.image_data, raw_pdf)
        self.assertEqual(pdf_rec.image_mime_type, "application/pdf")
        self.assertEqual(pdf_rec.image_filename, "itinerary.pdf")
        self.assertEqual(pdf_rec.currency, "EUR")


if __name__ == "__main__":
    unittest.main()
