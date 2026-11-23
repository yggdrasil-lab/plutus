"""Integration tests for private_note accounts, cumulative payout summaries, and counterpart PATCH."""

from datetime import date, timedelta
from pathlib import Path
import sys
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base, get_db
from app.main import app
from app.models import Account, AssetSnapshot, Category, Transaction
from app.services.category_taxonomy import ensure_default_categories
from app.services.fx_service import cache_exchange_rate

TEST_DB_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class TestPrivateNoteAndPayoutTracking(unittest.TestCase):
    def setUp(self) -> None:
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        ensure_default_categories(self.db)

        def override_get_db():
            db = TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

        self.acc_cash = Account(
            name="DBS Synthetic Savings",
            account_type="savings",
            tier="cash",
            institution="DBS Bank",
            currency="SGD",
        )
        self.acc_kiwisaver = Account(
            name="KiwiSaver Core",
            account_type="kiwisaver",
            tier="locked",
            institution="Simplicity",
            currency="NZD",
        )
        self.acc_nzd = Account(
            name="ASB Everyday",
            account_type="checking",
            tier="cash",
            institution="ASB",
            currency="NZD",
        )
        self.db.add_all([self.acc_cash, self.acc_kiwisaver, self.acc_nzd])
        self.db.commit()
        self.db.refresh(self.acc_cash)
        self.db.refresh(self.acc_kiwisaver)
        self.db.refresh(self.acc_nzd)

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_private_note_account_creation_patch_and_unknown_type_rejection(self) -> None:
        create_res = self.client.post(
            "/accounts",
            json={
                "name": "Alex Business Note",
                "account_type": "private_note",
                "tier": "locked",
                "institution": "Private Issuer",
                "currency": "SGD",
                "expected_annual_return": 0.102,
                "is_active": True,
            },
        )
        self.assertEqual(create_res.status_code, 201)
        created = create_res.json()
        self.assertEqual(created["account_type"], "private_note")
        self.assertEqual(created["tier"], "locked")
        self.assertAlmostEqual(created["expected_annual_return"], 0.102, places=4)
        self.assertEqual(created["payout_total_native"], 0.0)
        self.assertEqual(created["payout_total_nzd"], 0.0)
        self.assertEqual(created["payout_count"], 0)
        self.assertIsNone(created["first_payout_date"])
        self.assertIsNone(created["last_payout_date"])

        bad_create = self.client.post(
            "/accounts",
            json={
                "name": "Bad Note",
                "account_type": "promissory_bond",
                "tier": "locked",
                "institution": "Private Issuer",
                "currency": "SGD",
            },
        )
        self.assertEqual(bad_create.status_code, 422)

        patch_res = self.client.patch(
            f"/accounts/{self.acc_nzd.id}",
            json={"account_type": "private_note", "tier": "locked"},
        )
        self.assertEqual(patch_res.status_code, 200)
        self.assertEqual(patch_res.json()["account_type"], "private_note")

        bad_patch = self.client.patch(
            f"/accounts/{self.acc_nzd.id}",
            json={"account_type": "invalid_note_type"},
        )
        self.assertEqual(bad_patch.status_code, 422)

    def test_cumulative_payout_aggregation_beyond_pagination_subcategories_and_fx(self) -> None:
        note_acc = Account(
            name="Alex Business Note",
            account_type="private_note",
            tier="locked",
            institution="Private Issuer",
            currency="SGD",
            expected_annual_return=0.102,
        )
        broker_acc = Account(
            name="Global Dividend Plan",
            account_type="brokerage",
            tier="locked",
            institution="Apex Life",
            currency="SGD",
        )
        self.db.add_all([note_acc, broker_acc])
        self.db.flush()

        income_root = self.db.query(Category).filter(Category.name == "Income").one()
        div_child = Category(
            name="Note Interest",
            parent_id=income_root.id,
            is_system=False,
            is_active=True,
            sort_order=921,
        )
        self.db.add(div_child)
        self.db.flush()

        start_dt = date(2026, 1, 1)
        txs: list[Transaction] = []
        # 115 unlinked Income payouts across root "Income" and child "Note Interest" (> 100 pagination cap)
        for idx in range(115):
            tx_dt = start_dt + timedelta(days=idx)
            cat_name = "Note Interest" if idx % 2 == 1 else "Income"
            txs.append(
                Transaction(
                    transaction_date=tx_dt,
                    account_id=self.acc_cash.id,
                    counterpart_account_id=note_acc.id,
                    amount=10.0,
                    currency="SGD",
                    exchange_rate=1.30,
                    merchant="Alex Payout",
                    category=cat_name,
                    raw_hash=f"note-payout-{idx}",
                )
            )

        # Linked transfer pair MUST be excluded from payout totals
        linked_out = Transaction(
            transaction_date=date(2026, 5, 1),
            account_id=self.acc_cash.id,
            counterpart_account_id=note_acc.id,
            amount=500.0,
            currency="SGD",
            exchange_rate=1.0,
            merchant="Linked Transfer",
            category="Income",
            raw_hash="note-linked-exclude",
        )
        linked_in = Transaction(
            transaction_date=date(2026, 5, 1),
            account_id=note_acc.id,
            counterpart_account_id=self.acc_cash.id,
            amount=-500.0,
            currency="SGD",
            exchange_rate=1.0,
            merchant="Linked Transfer Partner",
            category="Transfers",
            raw_hash="note-linked-partner",
        )
        # Unlinked Transfers inflow and negative expense MUST be excluded
        transfer_in = Transaction(
            transaction_date=date(2026, 5, 2),
            account_id=self.acc_cash.id,
            counterpart_account_id=note_acc.id,
            amount=250.0,
            currency="SGD",
            exchange_rate=1.30,
            merchant="Unlinked Transfer",
            category="Transfers",
            raw_hash="note-transfer-exclude",
        )
        neg_adj = Transaction(
            transaction_date=date(2026, 5, 3),
            account_id=self.acc_cash.id,
            counterpart_account_id=note_acc.id,
            amount=-85.0,
            currency="SGD",
            exchange_rate=1.30,
            merchant="Fee Adjustment",
            category="Income",
            raw_hash="note-neg-exclude",
        )
        # Broker payout with missing tx.exchange_rate resolves cached ExchangeRate instead of 1:1
        cache_exchange_rate(
            self.db,
            rate_date=date(2026, 6, 1),
            from_currency="SGD",
            to_currency="NZD",
            rate=1.28,
            source="stub",
        )
        broker_div = Transaction(
            transaction_date=date(2026, 6, 1),
            account_id=self.acc_cash.id,
            counterpart_account_id=broker_acc.id,
            amount=100.0,
            currency="SGD",
            exchange_rate=None,
            merchant="Dividend Credit",
            category="Income",
            raw_hash="broker-cached-fx",
        )
        # NZD transaction with noisy exchange_rate must never multiply NZD by foreign rate
        nzd_div = Transaction(
            transaction_date=date(2026, 6, 2),
            account_id=self.acc_nzd.id,
            counterpart_account_id=broker_acc.id,
            amount=40.0,
            currency="NZD",
            exchange_rate=3.5,
            merchant="NZD Dividend",
            category="Note Interest",
            raw_hash="broker-nzd-no-mult",
        )

        self.db.add_all([*txs, linked_out, linked_in, transfer_in, neg_adj, broker_div, nzd_div])
        self.db.flush()
        linked_out.linked_transaction_id = linked_in.id
        linked_in.linked_transaction_id = linked_out.id
        self.db.commit()

        # Verify zero N+1 query scaling on GET /accounts
        select_queries: list[str] = []

        def _capture_sql(_conn, _cursor, statement, _params, _context, _executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                select_queries.append(statement)

        event.listen(engine, "before_cursor_execute", _capture_sql)
        try:
            list_res = self.client.get("/accounts")
        finally:
            event.remove(engine, "before_cursor_execute", _capture_sql)

        self.assertEqual(list_res.status_code, 200)
        # Expect <= 4 SELECTs (accounts, categories, grouped payouts, optional missing-FX lookup), never N+1 per account
        self.assertLessEqual(len(select_queries), 5)

        by_id = {row["id"]: row for row in list_res.json()}
        note_row = by_id[note_acc.id]
        self.assertEqual(note_row["payout_count"], 115)
        self.assertAlmostEqual(note_row["payout_total_native"], 1150.0, places=2)
        self.assertAlmostEqual(note_row["payout_total_nzd"], 1495.0, places=2)
        self.assertEqual(note_row["first_payout_date"], start_dt.isoformat())
        self.assertEqual(note_row["last_payout_date"], (start_dt + timedelta(days=114)).isoformat())

        broker_row = by_id[broker_acc.id]
        self.assertEqual(broker_row["payout_count"], 2)
        self.assertAlmostEqual(broker_row["payout_total_native"], 140.0, places=2)
        self.assertAlmostEqual(broker_row["payout_total_nzd"], round(100.0 * 1.28 + 40.0, 2), places=2)
        self.assertEqual(broker_row["first_payout_date"], "2026-06-01")
        self.assertEqual(broker_row["last_payout_date"], "2026-06-02")

        # GET /accounts/{id} and PATCH /accounts/{id} return identical summary fields
        get_res = self.client.get(f"/accounts/{note_acc.id}")
        self.assertEqual(get_res.status_code, 200)
        self.assertEqual(get_res.json()["payout_count"], 115)
        self.assertAlmostEqual(get_res.json()["payout_total_nzd"], 1495.0, places=2)

        patch_acc = self.client.patch(
            f"/accounts/{note_acc.id}",
            json={"expected_annual_return": 0.105},
        )
        self.assertEqual(patch_acc.status_code, 200)
        self.assertEqual(patch_acc.json()["payout_count"], 115)
        self.assertAlmostEqual(patch_acc.json()["payout_total_native"], 1150.0, places=2)

    def test_transaction_counterpart_patch_validation_and_type_hydration(self) -> None:
        note_acc = Account(
            name="Alex Business Note",
            account_type="private_note",
            tier="locked",
            institution="Private Issuer",
            currency="SGD",
        )
        broker_acc = Account(
            name="Global Dividend Plan",
            account_type="brokerage",
            tier="locked",
            institution="Apex Life",
            currency="SGD",
        )
        self.db.add_all([note_acc, broker_acc])
        self.db.flush()

        tx = Transaction(
            transaction_date=date.today(),
            account_id=self.acc_cash.id,
            amount=85.0,
            currency="SGD",
            exchange_rate=1.29,
            merchant="Monthly Note Payout",
            category="Income",
            tags='["note-payout"]',
            raw_hash="tx-patch-counterpart-1",
        )
        self.db.add(tx)
        self.db.commit()
        self.db.refresh(tx)

        # Reject nonexistent counterpart account (404 matching loan_id convention)
        missing_res = self.client.patch(
            f"/transactions/{tx.id}",
            json={"counterpart_account_id": 999999},
        )
        self.assertEqual(missing_res.status_code, 404)

        # Reject self-reference matching tx.account_id with HTTP 400
        self_res = self.client.patch(
            f"/transactions/{tx.id}",
            json={"counterpart_account_id": self.acc_cash.id},
        )
        self.assertEqual(self_res.status_code, 400)
        self.assertEqual(
            self_res.json()["detail"],
            "Counterpart account cannot match transaction account",
        )

        # Assign counterpart_account_id via PATCH (no mirrored transfer created)
        assign_res = self.client.patch(
            f"/transactions/{tx.id}",
            json={"counterpart_account_id": note_acc.id},
        )
        self.assertEqual(assign_res.status_code, 200)
        assigned = assign_res.json()
        self.assertEqual(assigned["counterpart_account_id"], note_acc.id)
        self.assertEqual(assigned["counterpart_account_name"], "Alex Business Note")
        self.assertEqual(assigned["counterpart_account_type"], "private_note")
        self.assertIsNone(assigned["linked_transaction_id"])
        self.assertEqual(self.db.query(Transaction).count(), 1)

        # Omitting counterpart_account_id in another PATCH preserves existing attribution
        notes_res = self.client.patch(
            f"/transactions/{tx.id}",
            json={"notes": "Verified payout"},
        )
        self.assertEqual(notes_res.status_code, 200)
        self.assertEqual(notes_res.json()["counterpart_account_id"], note_acc.id)
        self.assertEqual(notes_res.json()["counterpart_account_type"], "private_note")

        # GET /transactions (ledger and cashflow inflow mode) hydrates counterpart_account_type
        list_res = self.client.get(f"/transactions?account_id={note_acc.id}")
        self.assertEqual(list_res.status_code, 200)
        self.assertEqual(len(list_res.json()), 1)
        self.assertEqual(list_res.json()[0]["counterpart_account_name"], "Alex Business Note")
        self.assertEqual(list_res.json()[0]["counterpart_account_type"], "private_note")

        inflow_res = self.client.get(f"/transactions?account_id={note_acc.id}&flow=inflow")
        self.assertEqual(inflow_res.status_code, 200)
        self.assertEqual(inflow_res.json()[0]["counterpart_account_type"], "private_note")

        tag_res = self.client.get("/transactions?tag=note-payout")
        self.assertEqual(tag_res.status_code, 200)
        self.assertEqual(tag_res.json()[0]["counterpart_account_type"], "private_note")

        unfiltered_res = self.client.get("/transactions")
        self.assertEqual(unfiltered_res.status_code, 200)
        self.assertEqual(unfiltered_res.json()[0]["counterpart_account_type"], "private_note")

        cat_res = self.client.get("/transactions?category=Income")
        self.assertEqual(cat_res.status_code, 200)
        self.assertEqual(cat_res.json()[0]["counterpart_account_type"], "private_note")

        # Reassign to brokerage counterpart and verify updated hydration
        update_res = self.client.patch(
            f"/transactions/{tx.id}",
            json={"counterpart_account_id": broker_acc.id},
        )
        self.assertEqual(update_res.status_code, 200)
        self.assertEqual(update_res.json()["counterpart_account_id"], broker_acc.id)
        self.assertEqual(update_res.json()["counterpart_account_name"], "Global Dividend Plan")
        self.assertEqual(update_res.json()["counterpart_account_type"], "brokerage")

        # Clear counterpart_account_id explicitly with null
        clear_res = self.client.patch(
            f"/transactions/{tx.id}",
            json={"counterpart_account_id": None},
        )
        self.assertEqual(clear_res.status_code, 200)
        cleared = clear_res.json()
        self.assertIsNone(cleared["counterpart_account_id"])
        self.assertIsNone(cleared["counterpart_account_name"])
        self.assertIsNone(cleared["counterpart_account_type"])

    def test_runway_with_private_note_and_kiwisaver_locked_reserve(self) -> None:
        today = date.today()
        note_acc = Account(
            name="Alex Business Note",
            account_type="private_note",
            tier="locked",
            institution="Private Issuer",
            currency="SGD",
            expected_annual_return=0.102,
            is_active=True,
        )
        self.db.add(note_acc)
        self.db.flush()

        self.db.add_all(
            [
                AssetSnapshot(
                    snapshot_date=today,
                    account_id=self.acc_nzd.id,
                    balance=20000.0,
                    currency="NZD",
                    exchange_rate=1.0,
                    balance_nzd=20000.0,
                    source="manual",
                ),
                AssetSnapshot(
                    snapshot_date=today,
                    account_id=self.acc_kiwisaver.id,
                    balance=30000.0,
                    currency="NZD",
                    exchange_rate=1.0,
                    balance_nzd=30000.0,
                    source="manual",
                ),
                AssetSnapshot(
                    snapshot_date=today,
                    account_id=note_acc.id,
                    balance=10000.0,
                    currency="SGD",
                    exchange_rate=1.29,
                    balance_nzd=12900.0,
                    source="manual",
                ),
            ]
        )
        self.db.commit()

        runway_res = self.client.get("/runway")
        self.assertEqual(runway_res.status_code, 200)
        breakdown = runway_res.json()["breakdown"]
        self.assertAlmostEqual(breakdown["total_liquid_balance"], 20000.0, places=2)
        self.assertAlmostEqual(breakdown["locked_balance"], 42900.0, places=2)
        self.assertAlmostEqual(breakdown["net_worth"], 62900.0, places=2)
        self.assertEqual(breakdown["tier_labels"]["locked"], "Locked Reserve")

    def test_missing_fx_without_cached_rate_matches_to_nzd_fallback(self) -> None:
        note_acc = Account(
            name="Unrated Note",
            account_type="private_note",
            tier="locked",
            institution="Private Issuer",
            currency="USD",
        )
        self.db.add(note_acc)
        self.db.flush()

        self.db.add(
            Transaction(
                transaction_date=date(2026, 7, 1),
                account_id=self.acc_cash.id,
                counterpart_account_id=note_acc.id,
                amount=50.0,
                currency="USD",
                exchange_rate=None,
                merchant="USD Payout",
                category="Income",
                raw_hash="note-usd-no-cache",
            )
        )
        self.db.commit()

        res = self.client.get(f"/accounts/{note_acc.id}")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["payout_count"], 1)
        self.assertAlmostEqual(body["payout_total_native"], 50.0, places=2)
        self.assertAlmostEqual(body["payout_total_nzd"], 50.0, places=2)


if __name__ == "__main__":
    unittest.main()
