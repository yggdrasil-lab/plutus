"""Unit tests for Akahu, Wise, and DBS email alert connectors (SOLID / FakeGateway)."""

from datetime import date
from pathlib import Path
import sys
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.models import Account, AssetSnapshot, Transaction
from app.services.connectors.akahu import normalize_akahu_feeds, sync_akahu
from app.services.connectors.wise import normalize_wise_feed, sync_wise
from app.services.ingestion_service import (
    NormalizedTx,
    ProviderAccountFeed,
    ingest_normalized_transactions,
)

TEST_DB_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class FakeFeedGateway:
    """In-memory BankFeedGateway stub for zero-network unit testing."""

    def __init__(self, feeds: list[ProviderAccountFeed]) -> None:
        self._feeds = feeds

    def fetch_feeds(self, *, days: int) -> list[ProviderAccountFeed]:
        return self._feeds


class TestExternalConnectors(unittest.TestCase):
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

        self._orig_akahu_app = settings.akahu_app_token
        self._orig_akahu_user = settings.akahu_user_token
        self._orig_wise_token = settings.wise_api_token

    def tearDown(self):
        settings.akahu_app_token = self._orig_akahu_app
        settings.akahu_user_token = self._orig_akahu_user
        settings.wise_api_token = self._orig_wise_token
        self.db.close()
        Base.metadata.drop_all(bind=engine)
        app.dependency_overrides.clear()

    def test_sync_fail_loud_without_credentials(self):
        settings.akahu_app_token = ""
        settings.akahu_user_token = ""
        settings.wise_api_token = ""
        self.assertEqual(self.client.post("/integrations/sync/akahu").status_code, 503)
        self.assertEqual(self.client.post("/integrations/sync/wise").status_code, 503)
        self.assertEqual(self.client.post("/integrations/sync/unknown").status_code, 400)

    def test_akahu_normalizer_and_idempotent_sync(self):
        feeds = normalize_akahu_feeds(
            raw_accounts=[
                {
                    "_id": "acc_asb_1",
                    "name": "ASB Streamline",
                    "type": "CHECKING",
                    "connection": {"name": "ASB"},
                    "balance": {"current": 4500.25, "currency": "NZD"},
                }
            ],
            raw_transactions=[
                {
                    "_id": "tx_1",
                    "_account": "acc_asb_1",
                    "date": "2026-11-15T10:00:00Z",
                    "amount": -42.50,
                    "description": "PAK N SAVE ALBANY",
                    "merchant": {"name": "Pak n Save"},
                    "category": {"name": "Groceries"},
                },
                {
                    "_id": "tx_2",
                    "_account": "acc_asb_1",
                    "date": "2026-11-14T09:00:00Z",
                    "amount": 2500.00,
                    "description": "SALARY",
                    "category": {"name": "Income"},
                },
            ],
        )
        gateway = FakeFeedGateway(feeds)

        res1 = sync_akahu(self.db, days=14, gateway=gateway)
        self.assertEqual(res1.accounts_synced, 1)
        self.assertEqual(res1.transactions_created, 2)
        self.assertEqual(res1.snapshots_updated, 1)

        res2 = sync_akahu(self.db, days=14, gateway=gateway)
        self.assertEqual(res2.transactions_created, 0)
        self.assertEqual(res2.transactions_skipped, 2)

        snaps = self.db.scalars(select(AssetSnapshot)).all()
        self.assertEqual(len(snaps), 1)
        self.assertEqual(float(snaps[0].balance), 4500.25)

    def test_wise_normalizer_and_idempotent_sync(self):
        feed = normalize_wise_feed(
            profile={"id": 999, "type": "PERSONAL"},
            balance={
                "id": 101,
                "currency": "SGD",
                "type": "STANDARD",
                "amount": {"value": 1200.50, "currency": "SGD"},
            },
            raw_transactions=[
                {
                    "referenceNumber": "TRANSFER-1",
                    "date": "2026-11-15T12:00:00Z",
                    "amount": {"value": -500.00, "currency": "SGD"},
                    "totalFees": {"value": 3.25, "currency": "SGD"},
                    "exchangeDetails": {"rate": 1.245},
                    "details": {
                        "description": "Sent to NZD",
                        "recipient": {"name": "Raymund NZD"},
                    },
                }
            ],
        )
        gateway = FakeFeedGateway([feed])

        res1 = sync_wise(self.db, days=14, gateway=gateway)
        self.assertEqual(res1.accounts_synced, 1)
        self.assertEqual(res1.transactions_created, 1)
        self.assertEqual(res1.snapshots_updated, 1)

        tx = self.db.scalar(select(Transaction))
        self.assertIsNotNone(tx)
        self.assertEqual(tx.currency, "SGD")
        self.assertEqual(float(tx.fee_amount), 3.25)

        res2 = sync_wise(self.db, days=14, gateway=gateway)
        self.assertEqual(res2.transactions_created, 0)
        self.assertEqual(res2.transactions_skipped, 1)

    def test_dbs_alert_debit_credit_and_duplicate(self):
        debit_resp = self.client.post(
            "/integrations/dbs-alert",
            json={
                "subject": "DBS Card Transaction Alert",
                "text": "Card txn of SGD 18.50 at FAIRPRICE FINEST on 16 Nov 2026.",
            },
        )
        self.assertEqual(debit_resp.status_code, 200)
        data = debit_resp.json()
        self.assertEqual(data["status"], "created")
        self.assertFalse(data["duplicate"])
        self.assertEqual(float(data["transaction"]["amount"]), -18.50)
        self.assertEqual(data["transaction"]["currency"], "SGD")
        self.assertEqual(data["transaction"]["merchant"], "FAIRPRICE FINEST")

        dup_resp = self.client.post(
            "/integrations/dbs-alert",
            json={
                "subject": "DBS Card Transaction Alert",
                "text": "Card txn of SGD 18.50 at FAIRPRICE FINEST on 16 Nov 2026.",
            },
        )
        self.assertEqual(dup_resp.status_code, 200)
        self.assertTrue(dup_resp.json()["duplicate"])

        credit_resp = self.client.post(
            "/integrations/dbs-alert",
            json={
                "subject": "DBS PayNow Transfer Received",
                "text": "You have received SGD 250.00 from Alice Tan on 16 Nov 2026.",
            },
        )
        self.assertEqual(credit_resp.status_code, 200)
        self.assertEqual(float(credit_resp.json()["transaction"]["amount"]), 250.00)

        bad_resp = self.client.post(
            "/integrations/dbs-alert",
            json={"subject": "DBS Security Notice", "text": "Your OTP is 123456."},
        )
        self.assertEqual(bad_resp.status_code, 422)

    def test_cross_source_statement_upload_deduplicates_with_different_merchant(self):
        # 1. Akahu syncs Pak n Save + two legitimate $5.50 coffees on the same day
        feeds = normalize_akahu_feeds(
            raw_accounts=[
                {
                    "_id": "acc_asb_2",
                    "name": "ASB Everyday",
                    "type": "CHECKING",
                    "connection": {"name": "ASB"},
                    "balance": {"current": 1000.00, "currency": "NZD"},
                }
            ],
            raw_transactions=[
                {
                    "_id": "tx_pns",
                    "_account": "acc_asb_2",
                    "date": "2026-11-15T10:00:00Z",
                    "amount": -42.50,
                    "description": "PAK N SAVE",
                    "merchant": {"name": "Pak n Save"},
                },
                {
                    "_id": "tx_coffee_1",
                    "_account": "acc_asb_2",
                    "date": "2026-11-15T08:00:00Z",
                    "amount": -5.50,
                    "description": "COFFEE 1",
                    "merchant": {"name": "Cafe A"},
                },
                {
                    "_id": "tx_coffee_2",
                    "_account": "acc_asb_2",
                    "date": "2026-11-15T14:00:00Z",
                    "amount": -5.50,
                    "description": "COFFEE 2",
                    "merchant": {"name": "Cafe B"},
                },
            ],
        )
        res = sync_akahu(self.db, days=14, gateway=FakeFeedGateway(feeds))
        self.assertEqual(res.transactions_created, 3)

        # 2. Later, a CSV/PDF statement is uploaded for the same account with
        #    different merchant formatting, plus 1 genuinely new transaction
        account = self.db.scalar(select(Account).where(Account.external_account_id == "acc_asb_2"))
        statement_items = [
            NormalizedTx(
                transaction_date=date(2026, 11, 15),
                amount=-42.50,
                currency="NZD",
                merchant="PAK N SAVE ALBANY 0421",
            ),
            NormalizedTx(
                transaction_date=date(2026, 11, 15),
                amount=-5.50,
                currency="NZD",
                merchant="CAFE AUCKLAND POS 1",
            ),
            NormalizedTx(
                transaction_date=date(2026, 11, 15),
                amount=-5.50,
                currency="NZD",
                merchant="CAFE B POS 2",
            ),
            NormalizedTx(
                transaction_date=date(2026, 11, 15),
                amount=-19.00,
                currency="NZD",
                merchant="NEW PHARMACY TX",
            ),
        ]
        created, skipped, _ = ingest_normalized_transactions(self.db, account, statement_items)
        self.assertEqual(created, 1)
        self.assertEqual(skipped, 3)

    def test_zero_amount_transaction_does_not_crash_transfer_reconcile(self):
        feeds = normalize_akahu_feeds(
            raw_accounts=[
                {
                    "_id": "acc_zero",
                    "name": "ASB Card",
                    "type": "CREDITCARD",
                    "connection": {"name": "ASB"},
                    "balance": {"current": 0.0, "currency": "NZD"},
                }
            ],
            raw_transactions=[
                {
                    "_id": "tx_zero",
                    "_account": "acc_zero",
                    "date": "2026-11-15T10:00:00Z",
                    "amount": 0.00,
                    "description": "CARD AUTH CHECK",
                }
            ],
        )
        res = sync_akahu(self.db, days=14, gateway=FakeFeedGateway(feeds))
        self.assertEqual(res.transactions_created, 1)
