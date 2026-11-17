"""Unit tests for Akahu, Wise, and DBS email alert connectors (SOLID / FakeGateway)."""

from datetime import date, datetime
from pathlib import Path
import sys
from typing import Optional
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.models import Account, AssetSnapshot, Transaction
from app.services.connectors.akahu import (
    HttpAkahuGateway,
    normalize_akahu_feeds,
    sync_akahu,
)
from app.services.connectors.wise import (
    HttpWiseGateway,
    normalize_wise_feed,
    sync_wise,
)
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
        self.calls: list[dict] = []

    def fetch_feeds(
        self,
        *,
        days: int = 30,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> list[ProviderAccountFeed]:
        self.calls.append({"days": days, "start_date": start_date, "end_date": end_date})
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
                    "date": "2026-11-15T09:00:00Z",
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

    def test_existing_unlinked_account_is_auto_linked_not_duplicated(self):
        existing_anz = Account(
            name="ANZ Freedom",
            account_type="checking",
            tier="cash",
            institution="ANZ",
            currency="NZD",
            is_active=True,
        )
        self.db.add(existing_anz)
        self.db.commit()
        self.db.refresh(existing_anz)

        feeds = normalize_akahu_feeds(
            raw_accounts=[
                {
                    "_id": "acc_anz_live",
                    "name": "ANZ Go",
                    "type": "CHECKING",
                    "connection": {"name": "ANZ"},
                    "balance": {"current": 3200.00, "currency": "NZD"},
                }
            ],
            raw_transactions=[
                {
                    "_id": "tx_anz_1",
                    "_account": "acc_anz_live",
                    "date": "2026-11-16T08:00:00Z",
                    "amount": -12.00,
                    "description": "CAFE",
                }
            ],
        )
        res = sync_akahu(self.db, days=14, gateway=FakeFeedGateway(feeds))
        self.assertEqual(res.accounts_synced, 1)
        self.assertEqual(res.accounts[0].account_id, existing_anz.id)
        self.assertEqual(res.accounts[0].account_name, "ANZ Freedom")

        all_accounts = self.db.scalars(select(Account)).all()
        self.assertEqual(len(all_accounts), 1)
        self.assertEqual(all_accounts[0].external_provider, "akahu")
        self.assertEqual(all_accounts[0].external_account_id, "acc_anz_live")

    def test_sync_all_endpoint_passes_start_date_and_validates_range(self):
        from app.routers import integrations as integrations_router

        captured: list[tuple[str, int, Optional[date], Optional[date]]] = []

        def fake_akahu(db, days, start_date, end_date):
            captured.append(("akahu", days, start_date, end_date))
            return {
                "provider": "akahu",
                "accounts_synced": 1,
                "transactions_created": 2,
                "transactions_skipped": 0,
                "snapshots_updated": 1,
                "accounts": [],
            }

        def fake_wise(db, days, start_date, end_date):
            captured.append(("wise", days, start_date, end_date))
            return {
                "provider": "wise",
                "accounts_synced": 1,
                "transactions_created": 3,
                "transactions_skipped": 1,
                "snapshots_updated": 1,
                "accounts": [],
            }

        with patch.dict(
            integrations_router.SYNC_HANDLERS,
            {
                "akahu": lambda db, days, sd, ed: integrations_router.SyncResult(**fake_akahu(db, days, sd, ed)),
                "wise": lambda db, days, sd, ed: integrations_router.SyncResult(**fake_wise(db, days, sd, ed)),
            },
        ):
            resp = self.client.post("/integrations/sync/all?start_date=2024-08-01")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["provider"], "all")
        self.assertEqual(body["accounts_synced"], 2)
        self.assertEqual(body["transactions_created"], 5)
        self.assertEqual(body["transactions_skipped"], 1)
        self.assertEqual(
            captured,
            [
                ("akahu", 30, date(2024, 8, 1), None),
                ("wise", 30, date(2024, 8, 1), None),
            ],
        )

        bad_resp = self.client.post(
            "/integrations/sync/all?start_date=2026-11-18&end_date=2026-11-17"
        )
        self.assertEqual(bad_resp.status_code, 422)

    def test_http_wise_gateway_chunks_over_469_days_and_deduplicates_boundaries(self):
        gateway = HttpWiseGateway(api_token="test-wise-token")
        statement_calls: list[dict] = []

        boundary_tx = {
            "referenceNumber": "WISE-BOUNDARY-1",
            "date": "2025-08-01T00:00:00Z",
            "amount": {"value": -25.00, "currency": "SGD"},
            "details": {"description": "Boundary Coffee"},
        }
        no_ref_tx = {
            "date": "2025-08-01T00:00:00Z",
            "amount": {"value": -10.00, "currency": "SGD"},
            "details": {"description": "No Ref Boundary"},
        }

        def fake_get_json(_self, _client, url: str, _label: str, params=None):
            if url.endswith("/profiles"):
                return [{"id": 999, "type": "PERSONAL"}]
            if url.endswith("/balances"):
                return [
                    {
                        "id": 101,
                        "currency": "SGD",
                        "type": "STANDARD",
                        "amount": {"value": 500.0, "currency": "SGD"},
                    }
                ]
            if url.endswith("/statement.json"):
                statement_calls.append(dict(params or {}))
                idx = len(statement_calls)
                if idx == 1:
                    return {
                        "transactions": [
                            {
                                "referenceNumber": "WISE-W1-ONLY",
                                "date": "2024-09-15T10:00:00Z",
                                "amount": {"value": -40.00, "currency": "SGD"},
                                "details": {"description": "Window 1"},
                            },
                            boundary_tx,
                            no_ref_tx,
                        ]
                    }
                if idx == 2:
                    return {
                        "transactions": [
                            dict(boundary_tx),
                            dict(no_ref_tx),
                            {
                                "referenceNumber": "WISE-W2-ONLY",
                                "date": "2026-01-10T10:00:00Z",
                                "amount": {"value": -60.00, "currency": "SGD"},
                                "details": {"description": "Window 2"},
                            },
                        ]
                    }
                return {
                    "transactions": [
                        {
                            "referenceNumber": "WISE-W3-ONLY",
                            "date": "2026-10-05T10:00:00Z",
                            "amount": {"value": -15.00, "currency": "SGD"},
                            "details": {"description": "Window 3"},
                        }
                    ]
                }
            raise AssertionError(f"Unexpected URL: {url}")

        with patch.object(HttpWiseGateway, "_get_json", fake_get_json):
            feeds = gateway.fetch_feeds(
                start_date=date(2024, 8, 1),
                end_date=date(2026, 11, 17),
            )

        self.assertEqual(len(statement_calls), 3)
        for call_params in statement_calls:
            win_start = datetime.fromisoformat(call_params["intervalStart"].replace("Z", "+00:00"))
            win_end = datetime.fromisoformat(call_params["intervalEnd"].replace("Z", "+00:00"))
            self.assertLessEqual((win_end - win_start).days, 365)

        self.assertEqual(statement_calls[0]["intervalStart"], "2024-08-01T00:00:00.000Z")
        self.assertEqual(statement_calls[-1]["intervalEnd"], "2026-11-17T23:59:59.000Z")
        self.assertEqual(len(feeds), 1)
        self.assertEqual(len(feeds[0].transactions), 5)

    def test_http_akahu_gateway_uses_explicit_date_window(self):
        gateway = HttpAkahuGateway(app_token="app_tok", user_token="usr_tok")
        captured_dt: list[tuple[datetime, datetime]] = []

        def fake_get_accounts(_self, _client):
            return []

        def fake_get_transactions(_self, _client, *, start_dt: datetime, end_dt: datetime):
            captured_dt.append((start_dt, end_dt))
            return []

        with (
            patch.object(HttpAkahuGateway, "_get_accounts", fake_get_accounts),
            patch.object(HttpAkahuGateway, "_get_transactions", fake_get_transactions),
        ):
            gateway.fetch_feeds(start_date=date(2024, 8, 1), end_date=date(2024, 8, 31))

        self.assertEqual(len(captured_dt), 1)
        self.assertEqual(captured_dt[0][0].isoformat(), "2024-08-01T00:00:00+00:00")
        self.assertEqual(captured_dt[0][1].isoformat(), "2024-08-31T23:59:59+00:00")
