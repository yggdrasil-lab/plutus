"""Phase 3 tests for hardened DBS email alerts, SGT timezone rollover, account routing, and PDF dedup."""

from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.models import Account, IngestedEmail, Receipt, Transaction
from app.services.ingestion_service import NormalizedTx, ingest_normalized_transactions

TEST_DB_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class TestDbsEmailPhase3(unittest.TestCase):
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

        self._orig_map = settings.dbs_account_map
        self._orig_suffixes = settings.own_account_suffixes
        self._orig_llm_key = settings.llm_api_key
        self._orig_webhook_secret = settings.webhook_secret
        self._orig_env = settings.environment

        self.acc_savings = Account(
            name="DBS Multiplier Savings",
            account_type="savings",
            tier="cash",
            institution="DBS Bank",
            currency="SGD",
            account_number_last4="2222",
        )
        self.acc_cashline = Account(
            name="DBS Cashline",
            account_type="checking",
            tier="cash",
            institution="DBS Bank",
            currency="SGD",
            account_number_last4="3333",
        )
        self.acc_card = Account(
            name="DBS Live Fresh Visa",
            account_type="credit_card",
            tier="cash",
            institution="DBS Bank",
            currency="SGD",
            account_number_last4="4444",
        )
        self.acc_wise = Account(
            name="Wise SGD Account",
            account_type="savings",
            tier="cash",
            institution="Wise",
            currency="SGD",
            account_number_last4="1111",
        )
        self.db.add_all([self.acc_savings, self.acc_cashline, self.acc_card, self.acc_wise])
        self.db.commit()
        for acc in (self.acc_savings, self.acc_cashline, self.acc_card, self.acc_wise):
            self.db.refresh(acc)

    def tearDown(self):
        settings.dbs_account_map = self._orig_map
        settings.own_account_suffixes = self._orig_suffixes
        settings.llm_api_key = self._orig_llm_key
        settings.webhook_secret = self._orig_webhook_secret
        settings.environment = self._orig_env
        app.dependency_overrides.pop(get_db, None)
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_forwarded_gmail_fast_transfer_compact_currency_cashline_and_wise_reconcile(self):
        settings.dbs_account_map = json.dumps(
            {
                "2222": self.acc_savings.id,
                "3333": self.acc_cashline.id,
                "4444": self.acc_card.id,
            }
        )
        settings.own_account_suffixes = "1111,2222,3333,4444"

        wise_inflow = Transaction(
            transaction_date=date(2026, 11, 6),
            account_id=self.acc_wise.id,
            amount=2000.00,
            currency="SGD",
            merchant="Wise Top-up from DBS",
            category="General",
            raw_hash="wise-inflow-sgd-2000",
            notes="wise:tx-fast-2000",
        )
        self.db.add(wise_inflow)
        self.db.commit()
        self.db.refresh(wise_inflow)

        forwarded_body = (
            "---------- Forwarded message ---------\n"
            "From: DBS digibank Alert <ibanking@dbs.com>\n"
            "Date: Fri, Nov 6, 2026 at 8:18 PM\n"
            "Subject: DBS digibank Alert\n"
            "To: <alex@example.com>\n\n"
            "*Transaction Ref:* FAST202611060099\n"
            "*Date & Time:* 06 Nov 15:17 (SGT)\n"
            "*Amount:* SGD2000.00\n"
            "*From:* DBS Cashline A/C ending 3333\n"
            "*To:* Wise Account ending 1111\n"
        )

        resp = self.client.post(
            "/integrations/email",
            json={
                "email_id": 101,
                "message_id": "<fast-2000@example.com>",
                "route_target": "plutus_dbs_alert",
                "from_address": "alex@example.com",
                "to_address": "iris@example.com",
                "subject": "Fwd: DBS digibank Alert",
                "received_at": "2026-11-06T07:18:00Z",
                "text_body": forwarded_body,
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "processed")
        self.assertFalse(body["duplicate"])
        tx_id = body["transaction_id"]
        self.assertIsNotNone(tx_id)
        self.assertEqual(body["consumer_ref"], f"tx:{tx_id}")

        tx = self.db.get(Transaction, tx_id)
        self.db.refresh(tx)
        self.db.refresh(wise_inflow)
        self.assertEqual(tx.account_id, self.acc_cashline.id)
        self.assertEqual(tx.transaction_date, date(2026, 11, 6))
        self.assertEqual(float(tx.amount), -2000.00)
        self.assertEqual(tx.currency, "SGD")
        self.assertEqual(tx.category, "Transfers")
        self.assertEqual(tx.notes, "dbs:FAST202611060099")
        self.assertEqual(tx.linked_transaction_id, wise_inflow.id)
        self.assertEqual(wise_inflow.linked_transaction_id, tx.id)
        self.assertEqual(wise_inflow.category, "Transfers")

        # Duplicate replay on same message_id short-circuits idempotently
        replay = self.client.post(
            "/integrations/email",
            json={
                "email_id": 101,
                "message_id": "<fast-2000@example.com>",
                "route_target": "plutus_dbs_alert",
                "subject": "Fwd: DBS digibank Alert",
                "text_body": forwarded_body,
            },
        )
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["duplicate"])
        self.assertEqual(replay.json()["transaction_id"], tx_id)

    def test_forwarded_midnight_nzdt_to_sgt_rollover_and_pdf_dedup_both_directions(self):
        # Direction 1: Generic PDF row arrives FIRST on 2026-10-29 (+2d settlement),
        # then forwarded DBS alert arrives for 27 Oct 19:02 (SGT) (vs 28 Oct +1300 header).
        created_pdf, _, pdf_txs = ingest_normalized_transactions(
            self.db,
            self.acc_savings,
            [
                NormalizedTx(
                    transaction_date=date(2026, 10, 29),
                    amount=-117.78,
                    currency="SGD",
                    merchant="Advice Bill Payment",
                    raw_description="Advice Bill Payment",
                    category="General",
                )
            ],
            skip_llm=True,
        )
        self.assertEqual(created_pdf, 1)
        pdf_tx_id = pdf_txs[0].id

        # Seed an unlinked receipt that should reconcile via reconcile_needed=True
        rec = Receipt(
            receipt_date=date(2026, 10, 29),
            merchant="M1 LIMITED",
            total_amount=117.78,
            currency="SGD",
            status="unlinked",
        )
        self.db.add(rec)
        self.db.commit()
        self.db.refresh(rec)

        forwarded_rollover = (
            "---------- Forwarded message ---------\n"
            "From: DBS Bank <ibanking@dbs.com>\n"
            "Date: Wed, 28 Oct 2026 00:05:00 +1300\n"
            "Subject: Bill Payment Alert\n"
            "To: <alex@example.com>\n\n"
            "Transaction Ref: BP202610271177\n"
            "Date & Time: 27 Oct 19:02 (SGT)\n"
            "Amount: SGD 117.78\n"
            "From: DBS Multiplier A/C ending 2222\n"
            "To: M1 LIMITED\n"
        )
        resp = self.client.post(
            "/integrations/email",
            json={
                "email_id": 102,
                "message_id": "<bp-rollover-1@example.com>",
                "route_target": "plutus_dbs_alert",
                "subject": "Fwd: Bill Payment Alert",
                "received_at": "2026-10-27T11:05:00Z",
                "text_body": forwarded_rollover,
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["transaction_id"], pdf_tx_id)

        all_txs = self.db.scalars(
            select(Transaction).where(Transaction.account_id == self.acc_savings.id)
        ).all()
        self.assertEqual(len(all_txs), 1)
        enriched = all_txs[0]
        self.assertEqual(enriched.merchant, "M1 LIMITED")
        self.assertIn("dbs:BP202610271177", enriched.notes or "")

        self.db.refresh(rec)
        self.assertEqual(rec.status, "matched")
        self.assertEqual(rec.transaction_id, pdf_tx_id)

        # Direction 2: Alert arrives FIRST on 2026-11-10, then PDF row arrives on 2026-11-12
        alert_resp = self.client.post(
            "/integrations/email",
            json={
                "email_id": 103,
                "message_id": "<card-alert-first@example.com>",
                "route_target": "plutus_dbs_alert",
                "subject": "DBS Card Alert",
                "received_at": "2026-11-10T04:00:00Z",
                "text_body": (
                    "Transaction Ref: CC20261110001\n"
                    "Date & Time: 10 Nov 12:00 (SGT)\n"
                    "Amount: SGD 45.80\n"
                    "From: DBS Live Fresh Visa Card ending 4444\n"
                    "To: SPOTIFY SINGAPORE\n"
                ),
            },
        )
        self.assertEqual(alert_resp.status_code, 200)
        card_tx_id = alert_resp.json()["transaction_id"]
        card_tx = self.db.get(Transaction, card_tx_id)
        self.assertEqual(card_tx.account_id, self.acc_card.id)
        self.assertEqual(float(card_tx.amount), -45.80)

        created2, skipped2, _ = ingest_normalized_transactions(
            self.db,
            self.acc_card,
            [
                NormalizedTx(
                    transaction_date=date(2026, 11, 12),
                    amount=-45.80,
                    currency="SGD",
                    merchant="Advice Card Settlement",
                    raw_description="Advice Card Settlement",
                )
            ],
            skip_llm=True,
        )
        self.assertEqual(created2, 0)
        self.assertEqual(skipped2, 1)
        self.db.refresh(card_tx)
        self.assertEqual(card_tx.merchant, "SPOTIFY SINGAPORE")
        self.assertEqual(card_tx.notes, "dbs:CC20261110001")

    def test_unfamiliar_dbs_template_uses_llm_fallback_and_handles_failures(self):
        settings.llm_api_key = "test-muninn-key"

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "is_transaction": True,
                                    "amount": 88.50,
                                    "currency": "SGD",
                                    "direction": "debit",
                                    "merchant": "FAIRPRICE XTRA",
                                    "transaction_date": "2026-11-14",
                                    "account_hint": "DBS Live Fresh Visa ending 4444",
                                    "transaction_ref": "LLM998877",
                                    "is_transfer": False,
                                }
                            )
                        }
                    }
                ]
            }
        ).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = False

        with patch("app.services.connectors.dbs_email.urllib.request.urlopen", return_value=mock_resp):
            ok = self.client.post(
                "/integrations/email",
                json={
                    "email_id": 104,
                    "message_id": "<unfamiliar-dbs-1@example.com>",
                    "route_target": "plutus_dbs_alert",
                    "subject": "DBS Notification",
                    "text_body": "Unstructured notice for eighty-eight fifty at FairPrice.",
                },
            )
        self.assertEqual(ok.status_code, 200)
        tx = self.db.get(Transaction, ok.json()["transaction_id"])
        self.assertEqual(tx.account_id, self.acc_card.id)
        self.assertEqual(tx.merchant, "FAIRPRICE XTRA")
        self.assertEqual(float(tx.amount), -88.50)
        self.assertEqual(tx.notes, "dbs:LLM998877")

        # Non-transaction LLM response -> 422 + failed IngestedEmail row
        otp_resp = MagicMock()
        otp_resp.read.return_value = json.dumps(
            {"choices": [{"message": {"content": json.dumps({"is_transaction": False, "amount": None})}}]}
        ).encode("utf-8")
        otp_resp.__enter__.return_value = otp_resp
        otp_resp.__exit__.return_value = False

        with patch("app.services.connectors.dbs_email.urllib.request.urlopen", return_value=otp_resp):
            bad = self.client.post(
                "/integrations/email",
                json={
                    "email_id": 105,
                    "message_id": "<unparseable-otp@example.com>",
                    "route_target": "plutus_dbs_alert",
                    "subject": "Security Notice",
                    "text_body": "Your verification code has expired.",
                },
            )
        self.assertEqual(bad.status_code, 422)
        failed_row = self.db.scalar(
            select(IngestedEmail).where(IngestedEmail.message_id == "<unparseable-otp@example.com>")
        )
        self.assertIsNotNone(failed_row)
        self.assertEqual(failed_row.status, "failed")


if __name__ == "__main__":
    unittest.main()
