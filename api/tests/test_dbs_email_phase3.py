"""Phase 3 tests for hardened DBS email alerts, SGT timezone rollover, account routing, and PDF dedup."""

from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings, settings
from app.db import Base, get_db
from app.main import app
from app.models import Account, IngestedEmail, Receipt, Transaction
from app.schemas import DbsAlertPayload
from app.services.connectors.dbs_email import (
    EXPLICIT_SUFFIX_RE,
    _text_matches_suffix,
    ingest_dbs_alert,
    parse_dbs_alert_details,
    resolve_dbs_account,
)
from app.services.email_utils import (
    extract_and_strip_forwarded_header,
    parse_forwarded_date,
    strip_invisible_unicode,
)
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

    def test_text_matches_suffix_matrix_and_no_filler_word_capture(self):
        positive_cases = [
            ("DBS Multiplier Account (A/C ending 1001)", "1001"),
            ("CashLine A/C ending 2002", "2002"),
            ("Jordan Lee A/C ending 4004", "4004"),
            ("Your DBS/ POSB account ending 1001", "1001"),
            ("DBS CASHLINE (Ref ending 2002)", "2002"),
            ("Account 100-1", "1001"),
        ]
        for text, suffix in positive_cases:
            with self.subTest(text=text, suffix=suffix):
                self.assertTrue(_text_matches_suffix(text, suffix, strict_marker=False))
                self.assertTrue(_text_matches_suffix(text, suffix, strict_marker=True))

        # Negative: Transaction Ref digits must not match in strict_marker=True (or embedded in longer ref)
        self.assertFalse(
            _text_matches_suffix("Transaction Ref: 99881001", "1001", strict_marker=True)
        )
        self.assertFalse(
            _text_matches_suffix("Transaction Ref: 99881001", "1001", strict_marker=False)
        )

        # Negative: never capture or match filler word "ending"
        self.assertEqual(
            EXPLICIT_SUFFIX_RE.findall("DBS Multiplier Account (A/C ending 1001)"),
            ["1001"],
        )
        self.assertIsNone(EXPLICIT_SUFFIX_RE.search("DBS Multiplier Account (A/C ending)"))
        self.assertFalse(
            _text_matches_suffix(
                "DBS Multiplier Account (A/C ending 1001)", "ending", strict_marker=True
            )
        )
        self.assertFalse(
            _text_matches_suffix("DBS Multiplier Account (A/C ending)", "1001", strict_marker=False)
        )

    def test_email_3_multiplier_to_cashline_dedups_generic_row_without_misroute(self):
        self.acc_savings.account_number_last4 = None
        self.acc_cashline.account_number_last4 = None
        self.acc_card.account_number_last4 = None
        self.db.commit()

        settings.dbs_account_map = json.dumps(
            {
                "1001": self.acc_savings.id,
                "1002": self.acc_savings.id,
                "2002": self.acc_cashline.id,
            }
        )
        settings.own_account_suffixes = "1001,1002,2002,4004"

        existing_tx = Transaction(
            transaction_date=date(2026, 10, 29),
            account_id=self.acc_savings.id,
            amount=-117.78,
            currency="SGD",
            merchant="Advice",
            category="Debt & Loans",
            raw_description="Advice",
            raw_hash="pdf-multiplier-advice-1",
            notes=None,
        )
        self.db.add(existing_tx)
        self.db.commit()
        self.db.refresh(existing_tx)

        email3_body = (
            "---------- Forwarded message ---------\n"
            "From: DBS digibank Alert <ibanking@dbs.com>\n"
            "Date: Wed, 28 Oct 2026 at 12:00\u202fAM\n"
            "Subject: DBS digibank Alert\n"
            "To: <alex@example.com>\n\n"
            "*Transaction Ref: *DBSREF900100200300\n"
            "*Date & Time: *27 Oct 19:02 (SGT)\n"
            "*Amount: *SGD 117.78\n"
            "*From: *DBS Multiplier Account (A/C ending 1001)\n"
            "*To: *DBS CASHLINE (Ref ending 2002)\n"
        )

        with patch(
            "app.services.ingestion_service.classify_transaction_jev_with_tags",
            return_value=("General", False, []),
        ):
            resp = self.client.post(
                "/integrations/email",
                json={
                    "email_id": 3,
                    "message_id": "<synthetic-email-3@example.com>",
                    "route_target": "plutus_dbs_alert",
                    "subject": "Fwd: DBS digibank Alert",
                    "received_at": "2026-10-27T11:00:00Z",
                    "text_body": email3_body,
                },
            )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["transaction_id"], existing_tx.id)

        self.db.expire_all()
        savings_txs = self.db.scalars(
            select(Transaction).where(Transaction.account_id == self.acc_savings.id)
        ).all()
        cashline_txs = self.db.scalars(
            select(Transaction).where(Transaction.account_id == self.acc_cashline.id)
        ).all()
        self.assertEqual(len(savings_txs), 1)
        self.assertEqual(len(cashline_txs), 0)

        enriched = savings_txs[0]
        self.assertEqual(enriched.id, existing_tx.id)
        self.assertEqual(enriched.account_id, self.acc_savings.id)
        self.assertEqual(enriched.notes, "dbs:DBSREF900100200300")

    def test_account_hint_keyword_precedence_over_combined_text_suffix(self):
        self.acc_savings.account_number_last4 = None
        self.acc_cashline.account_number_last4 = None
        self.acc_card.account_number_last4 = None
        self.db.commit()

        settings.dbs_account_map = json.dumps(
            {
                "1001": self.acc_savings.id,
                "2002": self.acc_cashline.id,
            }
        )

        resolved = resolve_dbs_account(
            self.db,
            currency="SGD",
            account_hint="DBS Multiplier Account",
            combined_text=(
                "From: DBS Multiplier Account\n"
                "To: DBS CASHLINE (Ref ending 2002)\n"
                "Amount: SGD 117.78"
            ),
        )
        self.assertEqual(resolved.id, self.acc_savings.id)

        with patch(
            "app.services.ingestion_service.classify_transaction_jev_with_tags",
            return_value=("General", False, []),
        ):
            result = ingest_dbs_alert(
                self.db,
                DbsAlertPayload(
                    subject="DBS digibank Alert",
                    text=(
                        "Transaction Ref: 8877665544\n"
                        "Date & Time: 27 Oct 19:02 (SGT)\n"
                        "Amount: SGD 117.78\n"
                        "From: DBS Multiplier Account\n"
                        "To: DBS CASHLINE (Ref ending 2002)\n"
                    ),
                ),
                received_at=datetime(2026, 10, 27, 11, 5, tzinfo=timezone.utc),
            )
        self.assertEqual(result.status, "created")
        self.assertIsNotNone(result.transaction)
        self.assertEqual(result.transaction.account_id, self.acc_savings.id)

    def test_email_4_paynow_credit_parses_sgt_and_enriches_preserving_link(self):
        self.acc_savings.account_number_last4 = None
        self.acc_cashline.account_number_last4 = None
        self.acc_card.account_number_last4 = None
        self.db.commit()

        settings.dbs_account_map = json.dumps(
            {
                "1001": self.acc_savings.id,
                "1002": self.acc_savings.id,
                "2002": self.acc_cashline.id,
            }
        )
        settings.own_account_suffixes = "1001,1002,2002,4004"

        wise_partner = Transaction(
            transaction_date=date(2026, 10, 27),
            account_id=self.acc_wise.id,
            amount=-651.37,
            currency="SGD",
            merchant="Wise SGD Outflow",
            category="Transfers",
            raw_hash="wise-partner-synthetic",
            notes="wise:tx-synthetic",
        )
        self.db.add(wise_partner)
        self.db.commit()
        self.db.refresh(wise_partner)

        existing_tx = Transaction(
            transaction_date=date(2026, 10, 28),
            account_id=self.acc_savings.id,
            amount=651.37,
            currency="SGD",
            merchant="Advice FAST Payment / Receipt",
            category="Transfers",
            raw_description="Advice FAST Payment / Receipt",
            raw_hash="pdf-multiplier-fast-1",
            notes=None,
            linked_transaction_id=wise_partner.id,
        )
        self.db.add(existing_tx)
        self.db.commit()
        self.db.refresh(existing_tx)
        wise_partner.linked_transaction_id = existing_tx.id
        self.db.commit()

        email4_body = (
            "---------- Forwarded message ---------\n"
            "From: DBS digibank Alert <ibanking@dbs.com>\n"
            "Date: Wed, 28 Oct 2026 at 12:00\u202fAM\n"
            "Subject: PayNow Transfer Alert\n"
            "To: <alex@example.com>\n\n"
            "Transaction Ref: TW00000000000011223344C120630614240\n"
            "You have received SGD 651.37 via PayNow on 27 Oct 2026 19:00 SGT.\n"
            "* From:*\u202fJordan Lee\n"
            "*To:*\u202fYour DBS/ POSB account ending 1001\n"
        )

        parsed = parse_dbs_alert_details(
            "Fwd: PayNow Transfer Alert",
            email4_body,
            received_at=datetime(2026, 10, 27, 11, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(parsed.direction, "credit")
        self.assertEqual(parsed.normalized.amount, 651.37)
        self.assertEqual(parsed.normalized.currency, "SGD")
        self.assertEqual(parsed.normalized.transaction_date, date(2026, 10, 27))
        self.assertEqual(parsed.account_hint, "Your DBS/ POSB account ending 1001")
        self.assertEqual(parsed.normalized.notes, "dbs:TW00000000000011223344C120630614240")

        with patch(
            "app.services.ingestion_service.classify_transaction_jev_with_tags",
            return_value=("Income", False, []),
        ):
            resp = self.client.post(
                "/integrations/email",
                json={
                    "email_id": 4,
                    "message_id": "<synthetic-email-4@example.com>",
                    "route_target": "plutus_dbs_alert",
                    "subject": "Fwd: PayNow Transfer Alert",
                    "received_at": "2026-10-27T11:00:00Z",
                    "text_body": email4_body,
                },
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["transaction_id"], existing_tx.id)

        self.db.expire_all()
        savings_txs = self.db.scalars(
            select(Transaction).where(Transaction.account_id == self.acc_savings.id)
        ).all()
        self.assertEqual(len(savings_txs), 1)
        enriched = savings_txs[0]
        self.assertEqual(enriched.id, existing_tx.id)
        self.assertEqual(enriched.account_id, self.acc_savings.id)
        self.assertEqual(enriched.category, "Transfers")
        self.assertEqual(enriched.linked_transaction_id, wise_partner.id)
        self.assertEqual(enriched.notes, "dbs:TW00000000000011223344C120630614240")

    def test_email_2_cashline_to_own_suffix_marks_transfers(self):
        self.acc_savings.account_number_last4 = None
        self.acc_cashline.account_number_last4 = None
        self.acc_card.account_number_last4 = None
        self.db.commit()

        settings.dbs_account_map = json.dumps(
            {
                "1001": self.acc_savings.id,
                "1002": self.acc_savings.id,
                "2002": self.acc_cashline.id,
            }
        )
        with patch.dict(
            os.environ,
            {"PLUTUS_OWN_ACCOUNT_SUFFIXES": "1001,1002,2002,4004"},
        ):
            env_settings = Settings()
            self.assertIn("4004", env_settings.resolved_own_account_suffixes())
            settings.own_account_suffixes = env_settings.own_account_suffixes

        email2_body = (
            "---------- Forwarded message ---------\n"
            "From: DBS digibank Alert <ibanking@dbs.com>\n"
            "Date: Fri, 6 Nov 2026 at 8:17\u202fPM\n"
            "Subject: DBS digibank Alert\n"
            "To: <alex@example.com>\n\n"
            "Transaction Ref: FAST202611062000\n"
            "Date & Time: 06 Nov 15:17 (SGT)\n"
            "Amount: SGD2000.00\n"
            "From: CashLine A/C ending 2002\n"
            "To: Alex Tan A/C ending 4004\n"
        )

        with patch(
            "app.services.ingestion_service.classify_transaction_jev_with_tags",
            return_value=("General", False, []),
        ):
            resp = self.client.post(
                "/integrations/email",
                json={
                    "email_id": 2,
                    "message_id": "<synthetic-email-2@example.com>",
                    "route_target": "plutus_dbs_alert",
                    "subject": "Fwd: DBS digibank Alert",
                    "received_at": "2026-11-06T07:17:00Z",
                    "text_body": email2_body,
                },
            )

        self.assertEqual(resp.status_code, 200)
        tx = self.db.get(Transaction, resp.json()["transaction_id"])
        self.assertIsNotNone(tx)
        self.assertEqual(tx.account_id, self.acc_cashline.id)
        self.assertEqual(tx.category, "Transfers")
        self.assertEqual(float(tx.amount), -2000.00)
        self.assertEqual(tx.currency, "SGD")
        self.assertEqual(tx.transaction_date, date(2026, 11, 6))

    def test_parse_forwarded_date_gmail_nz_sg_with_nnbsp(self):
        self.assertEqual(
            strip_invisible_unicode("12:00\u202fAM\xa0SGT"),
            "12:00 AM SGT",
        )
        self.assertEqual(
            parse_forwarded_date("Wed, 28 Oct 2026 at 12:00\u202fAM"),
            datetime(2026, 10, 28, 0, 0),
        )
        self.assertEqual(
            parse_forwarded_date("Fri, 6 Nov 2026 at 8:17\u202fPM"),
            datetime(2026, 11, 6, 20, 17),
        )
        self.assertEqual(
            parse_forwarded_date("28 Oct 2026 at 12:00 AM"),
            datetime(2026, 10, 28, 0, 0),
        )

    def test_nested_double_forwarded_gmail_strips_all_blocks_and_prefers_innermost_meta(self):
        double_fwd_body = (
            "---------- Forwarded message ---------\n"
            "From: Jordan Taylor <jordan@example.com>\n"
            "Date: Sat, 21 Nov 2026 at 10:00\u202fAM\n"
            "Subject: Fwd: Your Synthetic Store Order\n"
            "To: <archive@example.com>\n\n"
            "---------- Forwarded message ---------\n"
            "From: Synthetic Store <orders@store.example.com>\n"
            "Date: Thu, 12 Nov 2026 at 3:15\u202fPM\n"
            "Subject: Your Synthetic Store Order\n"
            "To: <jordan@example.com>\n\n"
            "Order #SYN-9001\n"
            "Total: NZD 42.50\n"
        )
        subj, body, ref_dt = extract_and_strip_forwarded_header(
            "Fwd: Fwd: Your Synthetic Store Order",
            double_fwd_body,
            datetime(2026, 11, 21, 1, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(subj, "Your Synthetic Store Order")
        self.assertEqual(body, "Order #SYN-9001\nTotal: NZD 42.50")
        self.assertNotIn("Forwarded message", body)
        self.assertEqual(ref_dt.date(), date(2026, 11, 12))

    def test_nested_forwarded_preserves_outer_meta_when_inner_lacks_it(self):
        partial_inner = (
            "---------- Forwarded message ---------\n"
            "From: Alex Morgan <alex@example.com>\n"
            "Date: Fri, 13 Nov 2026 at 9:30 AM\n"
            "Subject: Fw: Receipt SYN-9002\n"
            "To: <archive@example.com>\n\n"
            "---------- Forwarded message ---------\n"
            "From: Merchant Dispatch <dispatch@example.com>\n\n"
            "Item: Widget A\n"
        )
        subj, body, ref_dt = extract_and_strip_forwarded_header(
            "Fw: Fwd: Receipt SYN-9002",
            partial_inner,
            datetime(2026, 11, 21, 5, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(subj, "Receipt SYN-9002")
        self.assertEqual(body, "Item: Widget A")
        self.assertEqual(ref_dt.date(), date(2026, 11, 13))


if __name__ == "__main__":
    unittest.main()
