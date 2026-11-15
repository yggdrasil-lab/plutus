"""Unit tests for Akahu, Wise, and DBS email alert connectors."""

import json
from pathlib import Path
import sys
import unittest

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.models import Account, AssetSnapshot, Transaction
from app.services.connectors.akahu import sync_akahu
from app.services.connectors.wise import sync_wise

TEST_DB_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


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

    def test_sync_akahu_fail_loud_without_credentials(self):
        settings.akahu_app_token = ""
        settings.akahu_user_token = ""
        resp = self.client.post("/integrations/sync/akahu")
        self.assertEqual(resp.status_code, 503)

    def test_sync_wise_fail_loud_without_credentials(self):
        settings.wise_api_token = ""
        resp = self.client.post("/integrations/sync/wise")
        self.assertEqual(resp.status_code, 503)

    def test_akahu_sync_paginated_and_idempotent(self):
        settings.akahu_app_token = "app_tok_test"
        settings.akahu_user_token = "user_tok_test"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/accounts":
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "_id": "acc_asb_1",
                                "name": "ASB Streamline",
                                "type": "CHECKING",
                                "connection": {"name": "ASB"},
                                "balance": {"current": 4500.25, "currency": "NZD"},
                            }
                        ]
                    },
                )
            if request.url.path == "/v1/transactions":
                cursor = request.url.params.get("cursor")
                if not cursor:
                    return httpx.Response(
                        200,
                        json={
                            "items": [
                                {
                                    "_id": "tx_1",
                                    "_account": "acc_asb_1",
                                    "date": "2026-11-15T10:00:00Z",
                                    "amount": -42.50,
                                    "description": "PAK N SAVE ALBANY",
                                    "merchant": {"name": "Pak n Save"},
                                    "category": {"name": "Groceries"},
                                }
                            ],
                            "cursor": {"next": "page_2"},
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "_id": "tx_2",
                                "_account": "acc_asb_1",
                                "date": "2026-11-14T09:00:00Z",
                                "amount": 2500.00,
                                "description": "SALARY",
                                "category": {"name": "Income"},
                            }
                        ],
                        "cursor": {"next": None},
                    },
                )
            return httpx.Response(404)

        mock_client = httpx.Client(transport=httpx.MockTransport(handler))
        res1 = sync_akahu(self.db, days=14, client=mock_client)
        self.assertEqual(res1.accounts_synced, 1)
        self.assertEqual(res1.transactions_created, 2)
        self.assertEqual(res1.snapshots_updated, 1)

        res2 = sync_akahu(self.db, days=14, client=mock_client)
        self.assertEqual(res2.transactions_created, 0)
        self.assertEqual(res2.transactions_skipped, 2)

        snaps = self.db.scalars(select(AssetSnapshot)).all()
        self.assertEqual(len(snaps), 1)
        self.assertEqual(float(snaps[0].balance), 4500.25)

    def test_wise_sync_and_idempotency(self):
        settings.wise_api_token = "wise_tok_test"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/profiles":
                return httpx.Response(200, json=[{"id": 999, "type": "PERSONAL"}])
            if request.url.path == "/v1/profiles/999/balances":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": 101,
                            "currency": "SGD",
                            "type": "STANDARD",
                            "amount": {"value": 1200.50, "currency": "SGD"},
                        }
                    ],
                )
            if request.url.path == "/v1/profiles/999/balance-statements/101/statement.json":
                return httpx.Response(
                    200,
                    json={
                        "transactions": [
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
                        ]
                    },
                )
            return httpx.Response(404)

        mock_client = httpx.Client(transport=httpx.MockTransport(handler))
        res1 = sync_wise(self.db, days=14, client=mock_client)
        self.assertEqual(res1.accounts_synced, 1)
        self.assertEqual(res1.transactions_created, 1)
        self.assertEqual(res1.snapshots_updated, 1)

        tx = self.db.scalar(select(Transaction))
        self.assertIsNotNone(tx)
        self.assertEqual(tx.currency, "SGD")
        self.assertEqual(float(tx.fee_amount), 3.25)

        res2 = sync_wise(self.db, days=14, client=mock_client)
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

        # Duplicate alert should not create a second transaction
        dup_resp = self.client.post(
            "/integrations/dbs-alert",
            json={
                "subject": "DBS Card Transaction Alert",
                "text": "Card txn of SGD 18.50 at FAIRPRICE FINEST on 16 Nov 2026.",
            },
        )
        self.assertEqual(dup_resp.status_code, 200)
        self.assertTrue(dup_resp.json()["duplicate"])

        # Incoming credit alert
        credit_resp = self.client.post(
            "/integrations/dbs-alert",
            json={
                "subject": "DBS PayNow Transfer Received",
                "text": "You have received SGD 250.00 from Alice Tan on 16 Nov 2026.",
            },
        )
        self.assertEqual(credit_resp.status_code, 200)
        self.assertEqual(float(credit_resp.json()["transaction"]["amount"]), 250.00)

        # Unparseable payload fails loud with 422
        bad_resp = self.client.post(
            "/integrations/dbs-alert",
            json={"subject": "DBS Security Notice", "text": "Your OTP is 123456."},
        )
        self.assertEqual(bad_resp.status_code, 422)
