"""Unit tests for counterpart_account_id auto-mirroring, dividend flows, transaction hydration, and FX fixes."""

from datetime import date
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base, get_db
from app.main import app
from app.models import Account, AssetSnapshot, Transaction
from app.services.fx_service import cache_exchange_rate
from app.services.ingestion_service import NormalizedTx, ingest_normalized_transactions

TEST_DB_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class TestCounterpartAccountFlow(unittest.TestCase):
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

        self.acc_dbs = Account(
            name="DBS Multiplier",
            account_type="checking",
            tier="cash",
            institution="DBS Bank",
            currency="SGD",
        )
        self.acc_manulife = Account(
            name="Manulife",
            account_type="brokerage",
            tier="locked",
            institution="Manulife",
            currency="SGD",
        )
        self.acc_asb = Account(
            name="ASB Checking",
            account_type="checking",
            tier="cash",
            institution="ASB",
            currency="NZD",
        )
        self.db.add_all([self.acc_dbs, self.acc_manulife, self.acc_asb])
        self.db.commit()
        self.db.refresh(self.acc_dbs)
        self.db.refresh(self.acc_manulife)
        self.db.refresh(self.acc_asb)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_rules_counterpart_account_validation_hydration_and_clearing(self):
        bad_create = self.client.post(
            "/rules",
            json={
                "name": "Missing Account Rule",
                "description_pattern": "MANULIFE",
                "target_category": "Transfers",
                "counterpart_account_id": 99999,
            },
        )
        self.assertEqual(bad_create.status_code, 404)

        create_res = self.client.post(
            "/rules",
            json={
                "name": "Manulife GIRO",
                "description_pattern": "MANULIFE",
                "match_type": "contains",
                "direction": "outflow",
                "target_category": "Transfers",
                "target_tags": ["insurance", "savings"],
                "rename_merchant": "Manulife Plan",
                "counterpart_account_id": self.acc_manulife.id,
                "apply_retroactively": False,
            },
        )
        self.assertEqual(create_res.status_code, 201)
        created = create_res.json()
        rule_id = created["id"]
        self.assertEqual(created["counterpart_account_id"], self.acc_manulife.id)
        self.assertEqual(created["counterpart_account_name"], "Manulife")
        self.assertEqual(created["target_tags"], ["insurance", "savings"])

        list_res = self.client.get("/rules")
        self.assertEqual(list_res.status_code, 200)
        self.assertEqual(list_res.json()[0]["counterpart_account_name"], "Manulife")

        get_res = self.client.get(f"/rules/{rule_id}")
        self.assertEqual(get_res.status_code, 200)
        self.assertEqual(get_res.json()["counterpart_account_name"], "Manulife")

        bad_update = self.client.put(
            f"/rules/{rule_id}",
            json={"counterpart_account_id": 88888},
        )
        self.assertEqual(bad_update.status_code, 404)

        clear_res = self.client.put(
            f"/rules/{rule_id}",
            json={
                "target_tags": None,
                "rename_merchant": "",
                "counterpart_account_id": None,
            },
        )
        self.assertEqual(clear_res.status_code, 200)
        cleared = clear_res.json()
        self.assertEqual(cleared["target_tags"], [])
        self.assertIsNone(cleared["rename_merchant"])
        self.assertIsNone(cleared["counterpart_account_id"])
        self.assertIsNone(cleared["counterpart_account_name"])

    def test_transfer_rule_auto_mirror_idempotent_and_self_guard(self):
        d = date(2026, 11, 10)
        cache_exchange_rate(
            self.db,
            rate_date=d,
            from_currency="SGD",
            to_currency="NZD",
            rate=1.29,
            source="stub",
        )
        tx_giro = Transaction(
            transaction_date=d,
            account_id=self.acc_dbs.id,
            amount=-300.00,
            currency="SGD",
            merchant="GIRO",
            raw_description="GIRO PAYMENT TO MANULIFE",
            category="General",
            exchange_rate=1.29,
            raw_hash="dbs-manulife-giro-1",
            tags='["policy"]',
        )
        # Same-account transaction must trigger guard and never self-mirror
        tx_same_acc = Transaction(
            transaction_date=d,
            account_id=self.acc_manulife.id,
            amount=-100.00,
            currency="SGD",
            merchant="MANULIFE INTERNAL",
            raw_description="MANULIFE ADJUSTMENT",
            category="General",
            raw_hash="manulife-self-1",
        )
        self.db.add_all([tx_giro, tx_same_acc])
        self.db.commit()

        res = self.client.post(
            "/rules",
            json={
                "name": "Manulife Premium",
                "description_pattern": "MANULIFE",
                "match_type": "contains",
                "direction": "outflow",
                "target_category": "Transfers",
                "rename_merchant": "Manulife GIRO",
                "counterpart_account_id": self.acc_manulife.id,
                "apply_retroactively": True,
            },
        )
        self.assertEqual(res.status_code, 201)

        all_txs = self.db.scalars(select(Transaction).order_by(Transaction.id.asc())).all()
        self.assertEqual(len(all_txs), 3)

        self.db.refresh(tx_giro)
        self.db.refresh(tx_same_acc)
        mirror = next(t for t in all_txs if t.id not in (tx_giro.id, tx_same_acc.id))

        self.assertEqual(tx_giro.category, "Transfers")
        self.assertEqual(tx_giro.merchant, "Manulife GIRO")
        self.assertEqual(tx_giro.counterpart_account_id, self.acc_manulife.id)
        self.assertEqual(tx_giro.linked_transaction_id, mirror.id)
        self.assertEqual(float(tx_giro.exchange_rate), 1.0)

        self.assertEqual(mirror.account_id, self.acc_manulife.id)
        self.assertEqual(mirror.counterpart_account_id, self.acc_dbs.id)
        self.assertEqual(mirror.linked_transaction_id, tx_giro.id)
        self.assertEqual(float(mirror.amount), 300.00)
        self.assertEqual(mirror.currency, "SGD")
        self.assertEqual(mirror.category, "Transfers")
        self.assertEqual(float(mirror.exchange_rate), 1.0)
        self.assertEqual(mirror.notes, f"mirror:{tx_giro.id}")

        # Guarded transaction on Manulife itself was not mirrored
        self.assertIsNone(tx_same_acc.linked_transaction_id)
        self.assertIsNone(tx_same_acc.counterpart_account_id)

        # Re-running apply-all produces zero duplicate mirrors
        reapply = self.client.post("/rules/apply-all")
        self.assertEqual(reapply.status_code, 200)
        self.assertEqual(reapply.json()["updated"], 0)
        self.assertEqual(len(self.db.scalars(select(Transaction)).all()), 3)

        # Ledger hydration checks
        manulife_rows = self.client.get(f"/transactions?account_id={self.acc_manulife.id}").json()
        mirrored_dto = next(r for r in manulife_rows if r["id"] == mirror.id)
        self.assertEqual(mirrored_dto["account_name"], "Manulife")
        self.assertEqual(mirrored_dto["linked_account_name"], "DBS Multiplier")
        self.assertEqual(mirrored_dto["linked_amount"], -300.0)
        self.assertEqual(mirrored_dto["linked_currency"], "SGD")
        self.assertEqual(mirrored_dto["counterpart_account_id"], self.acc_dbs.id)
        self.assertEqual(mirrored_dto["counterpart_account_name"], "DBS Multiplier")
        # DBS linked leg must NOT appear in Manulife's ledger
        self.assertNotIn(tx_giro.id, [r["id"] for r in manulife_rows])

    def test_dividend_counterpart_flow_single_budget_count_and_account_query(self):
        today = date.today()
        cache_exchange_rate(
            self.db,
            rate_date=today,
            from_currency="SGD",
            to_currency="NZD",
            rate=1.29,
            source="stub",
        )

        # 1. Transfer rule (DBS -> Manulife) and 2. Dividend rule (Manulife -> DBS Income)
        self.client.post(
            "/rules",
            json={
                "name": "Manulife Contribution",
                "description_pattern": "MANULIFE",
                "direction": "outflow",
                "target_category": "Transfers",
                "counterpart_account_id": self.acc_manulife.id,
                "priority": 1,
                "apply_retroactively": False,
            },
        )
        self.client.post(
            "/rules",
            json={
                "name": "Manulife Dividend",
                "description_pattern": "MANULIFE",
                "direction": "inflow",
                "target_category": "Income",
                "rename_merchant": "Manulife Dividend",
                "target_tags": ["dividend"],
                "counterpart_account_id": self.acc_manulife.id,
                "priority": 2,
                "apply_retroactively": False,
            },
        )

        created, skipped, created_txs = ingest_normalized_transactions(
            self.db,
            self.acc_dbs,
            [
                NormalizedTx(
                    transaction_date=today,
                    amount=-300.00,
                    currency="SGD",
                    merchant="GIRO",
                    raw_description="GIRO TO MANULIFE",
                ),
                NormalizedTx(
                    transaction_date=today,
                    amount=65.54,
                    currency="SGD",
                    merchant="GIRO",
                    raw_description="GIRO FROM MANULIFE DIVIDEND",
                ),
            ],
            skip_llm=True,
        )
        self.assertEqual(created, 2)
        self.assertEqual(skipped, 0)

        giro_out = next(t for t in created_txs if float(t.amount) < 0)
        div_in = next(t for t in created_txs if float(t.amount) > 0)

        self.assertIsNotNone(giro_out.linked_transaction_id)
        self.assertEqual(giro_out.counterpart_account_id, self.acc_manulife.id)

        # Dividend stays Income, has counterpart_account_id, and NO mirror row
        self.assertEqual(div_in.category, "Income")
        self.assertEqual(div_in.merchant, "Manulife Dividend")
        self.assertEqual(div_in.counterpart_account_id, self.acc_manulife.id)
        self.assertIsNone(div_in.linked_transaction_id)
        self.assertEqual(len(self.db.scalars(select(Transaction)).all()), 3)

        # Manulife ledger shows its mirrored +300 transfer AND the unlinked +65.54 dividend
        manulife_ledger = self.client.get(f"/transactions?account_id={self.acc_manulife.id}").json()
        self.assertEqual(len(manulife_ledger), 2)
        div_dto = next(r for r in manulife_ledger if r["id"] == div_in.id)
        self.assertEqual(div_dto["account_name"], "DBS Multiplier")
        self.assertEqual(div_dto["counterpart_account_id"], self.acc_manulife.id)
        self.assertEqual(div_dto["counterpart_account_name"], "Manulife")
        self.assertIsNone(div_dto["linked_transaction_id"])
        self.assertIsNone(div_dto["linked_account_name"])

        # Tag filter query also hydrates counterpart and linked fields
        tag_filtered = self.client.get("/transactions?tag=dividend").json()
        self.assertEqual(len(tag_filtered), 1)
        self.assertEqual(tag_filtered[0]["counterpart_account_name"], "Manulife")

        # Budgets breakdown counts total_income exactly once
        breakdown = self.client.get("/budgets/breakdown?period=month").json()
        self.assertAlmostEqual(breakdown["total_income"], round(65.54 * 1.29, 2), places=2)
        self.assertEqual(breakdown["total_spend"], 0.0)

    def test_ingestion_pairs_real_counter_leg_before_counterpart_mirror(self):
        d = date(2026, 11, 12)
        self.client.post(
            "/rules",
            json={
                "name": "ASB to Manulife",
                "description_pattern": "MANULIFE",
                "direction": "outflow",
                "target_category": "Transfers",
                "counterpart_account_id": self.acc_manulife.id,
                "apply_retroactively": False,
            },
        )
        # Pre-existing real counter-leg on Manulife
        real_inflow = Transaction(
            transaction_date=d,
            account_id=self.acc_manulife.id,
            amount=300.00,
            currency="SGD",
            merchant="Transfer from DBS",
            raw_description="Direct Credit",
            category="Transfers",
            raw_hash="real-manulife-in-300",
        )
        self.db.add(real_inflow)
        self.db.commit()

        created, _, created_txs = ingest_normalized_transactions(
            self.db,
            self.acc_dbs,
            [
                NormalizedTx(
                    transaction_date=d,
                    amount=-300.00,
                    currency="SGD",
                    merchant="MANULIFE GIRO",
                    raw_description="MANULIFE GIRO",
                )
            ],
            skip_llm=True,
        )
        self.assertEqual(created, 1)
        tx_out = created_txs[0]
        self.db.refresh(real_inflow)
        # Paired with the real counter-leg; zero extra mirror rows created
        self.assertEqual(tx_out.linked_transaction_id, real_inflow.id)
        self.assertEqual(real_inflow.linked_transaction_id, tx_out.id)
        self.assertEqual(len(self.db.scalars(select(Transaction)).all()), 2)

    def test_snapshot_non_nzd_auto_fx_and_explicit_override(self):
        d = date(2026, 11, 16)
        cache_exchange_rate(
            self.db,
            rate_date=d,
            from_currency="SGD",
            to_currency="NZD",
            rate=1.29,
            source="stub",
        )

        # 1. Omitted exchange_rate on SGD snapshot auto-resolves 1.29 -> balance_nzd
        res_auto = self.client.post(
            f"/accounts/{self.acc_manulife.id}/snapshots",
            json={
                "snapshot_date": d.isoformat(),
                "account_id": self.acc_manulife.id,
                "balance": 14527.89,
                "currency": "SGD",
                "source": "manual",
                "notes": "Manulife valuation",
            },
        )
        self.assertEqual(res_auto.status_code, 201)
        auto_snap = res_auto.json()
        self.assertAlmostEqual(float(auto_snap["exchange_rate"]), 1.29, places=4)
        self.assertAlmostEqual(float(auto_snap["balance_nzd"]), round(14527.89 * 1.29, 2), places=2)

        runway = self.client.get("/runway").json()
        self.assertAlmostEqual(
            runway["breakdown"]["locked_balance"],
            round(14527.89 * 1.29, 2),
            places=2,
        )

        # 2. Explicit exchange_rate overrides lookup on same-date update
        res_explicit = self.client.post(
            f"/accounts/{self.acc_manulife.id}/snapshots",
            json={
                "snapshot_date": d.isoformat(),
                "account_id": self.acc_manulife.id,
                "balance": 14527.89,
                "currency": "SGD",
                "exchange_rate": 1.31,
                "source": "manual",
            },
        )
        self.assertEqual(res_explicit.status_code, 201)
        explicit_snap = res_explicit.json()
        self.assertAlmostEqual(float(explicit_snap["exchange_rate"]), 1.31, places=4)
        self.assertAlmostEqual(float(explicit_snap["balance_nzd"]), round(14527.89 * 1.31, 2), places=2)

        # 3. Offline fallback uses 1.0 only when get_exchange_rate returns None
        with patch("app.services.ingestion_service.get_exchange_rate", return_value=None):
            res_offline = self.client.post(
                f"/accounts/{self.acc_dbs.id}/snapshots",
                json={
                    "snapshot_date": d.isoformat(),
                    "account_id": self.acc_dbs.id,
                    "balance": 500.0,
                    "currency": "SGD",
                    "exchange_rate": None,
                    "source": "manual",
                },
            )
        self.assertEqual(res_offline.status_code, 201)
        self.assertEqual(float(res_offline.json()["exchange_rate"]), 1.0)
        self.assertEqual(float(res_offline.json()["balance_nzd"]), 500.0)

        # 4. NZD snapshot with omitted exchange_rate resolves to 1.0
        res_nzd = self.client.post(
            f"/accounts/{self.acc_asb.id}/snapshots",
            json={
                "snapshot_date": d.isoformat(),
                "account_id": self.acc_asb.id,
                "balance": 2000.0,
                "currency": "NZD",
                "source": "manual",
            },
        )
        self.assertEqual(res_nzd.status_code, 201)
        self.assertEqual(float(res_nzd.json()["exchange_rate"]), 1.0)
        self.assertEqual(float(res_nzd.json()["balance_nzd"]), 2000.0)

    def test_unlink_non_nzd_transfer_restores_fx_rate_and_preserves_non_transfer_fx(self):
        d = date(2026, 11, 14)
        cache_exchange_rate(
            self.db,
            rate_date=d,
            from_currency="SGD",
            to_currency="NZD",
            rate=1.29,
            source="stub",
        )
        tx_out = Transaction(
            transaction_date=d,
            account_id=self.acc_dbs.id,
            amount=-200.0,
            currency="SGD",
            merchant="DBS Transfer Out",
            category="Transfers",
            exchange_rate=1.0,
            raw_hash="unlink-sgd-out",
        )
        tx_in = Transaction(
            transaction_date=d,
            account_id=self.acc_manulife.id,
            amount=200.0,
            currency="SGD",
            merchant="Manulife Transfer In",
            category="Transfers",
            exchange_rate=1.0,
            raw_hash="unlink-sgd-in",
        )
        tx_food = Transaction(
            transaction_date=d,
            account_id=self.acc_dbs.id,
            amount=-15.0,
            currency="SGD",
            merchant="Hawker Stall",
            category="General",
            exchange_rate=1.29,
            raw_hash="non-transfer-sgd-food",
        )
        self.db.add_all([tx_out, tx_in, tx_food])
        self.db.flush()
        tx_out.linked_transaction_id = tx_in.id
        tx_in.linked_transaction_id = tx_out.id
        self.db.commit()

        # Patching tx_in off Transfers -> Income unlinks both SGD legs and restores SGD->NZD rate
        unlink_res = self.client.patch(
            f"/transactions/{tx_in.id}",
            json={"category": "Income"},
        )
        self.assertEqual(unlink_res.status_code, 200)
        unlinked_body = unlink_res.json()
        self.assertEqual(unlinked_body["category"], "Income")
        self.assertIsNone(unlinked_body["linked_transaction_id"])
        self.assertAlmostEqual(float(unlinked_body["exchange_rate"]), 1.29, places=4)
        self.assertIsNone(unlinked_body["linked_account_name"])

        self.db.expire_all()
        refreshed_out = self.db.get(Transaction, tx_out.id)
        self.assertIsNone(refreshed_out.linked_transaction_id)
        self.assertAlmostEqual(float(refreshed_out.exchange_rate), 1.29, places=4)

        # Patching a non-transfer SGD transaction preserves its existing FX rate
        food_res = self.client.patch(
            f"/transactions/{tx_food.id}",
            json={"category": "Dining"},
        )
        self.assertEqual(food_res.status_code, 200)
        self.assertAlmostEqual(float(food_res.json()["exchange_rate"]), 1.29, places=4)

    def test_create_transaction_hydrates_linked_and_counterpart_fields(self):
        d = date(2026, 11, 15)
        res_out = self.client.post(
            "/transactions",
            json={
                "transaction_date": d.isoformat(),
                "account_id": self.acc_dbs.id,
                "amount": -450.0,
                "currency": "SGD",
                "merchant": "Transfer to Manulife",
                "category": "Transfers",
            },
        )
        self.assertEqual(res_out.status_code, 201)
        out_id = res_out.json()["id"]

        res_in = self.client.post(
            "/transactions",
            json={
                "transaction_date": d.isoformat(),
                "account_id": self.acc_manulife.id,
                "amount": 450.0,
                "currency": "SGD",
                "merchant": "Transfer from DBS",
                "category": "Transfers",
            },
        )
        self.assertEqual(res_in.status_code, 201)
        in_body = res_in.json()
        self.assertEqual(in_body["linked_transaction_id"], out_id)
        self.assertEqual(in_body["linked_account_name"], "DBS Multiplier")
        self.assertEqual(in_body["linked_amount"], -450.0)
        self.assertEqual(in_body["linked_currency"], "SGD")

    def test_cross_currency_transfer_auto_mirror_and_relink_after_unlink(self):
        d = date(2026, 11, 13)
        cache_exchange_rate(
            self.db,
            rate_date=d,
            from_currency="SGD",
            to_currency="NZD",
            rate=1.28,
            source="stub",
        )
        tx_nzd = Transaction(
            transaction_date=d,
            account_id=self.acc_asb.id,
            amount=-1000.00,
            currency="NZD",
            merchant="Remit Manulife",
            raw_description="WISE MANULIFE TOPUP",
            category="General",
            raw_hash="asb-manulife-nzd-1",
        )
        self.db.add(tx_nzd)
        self.db.commit()

        res = self.client.post(
            "/rules",
            json={
                "name": "ASB to Manulife Cross Currency",
                "description_pattern": "MANULIFE TOPUP",
                "direction": "outflow",
                "target_category": "Transfers",
                "counterpart_account_id": self.acc_manulife.id,
                "apply_retroactively": True,
            },
        )
        self.assertEqual(res.status_code, 201)

        all_txs = self.db.scalars(select(Transaction).order_by(Transaction.id.asc())).all()
        self.assertEqual(len(all_txs), 2)
        self.db.refresh(tx_nzd)
        mirror = next(t for t in all_txs if t.id != tx_nzd.id)

        self.assertEqual(tx_nzd.linked_transaction_id, mirror.id)
        self.assertEqual(mirror.account_id, self.acc_manulife.id)
        self.assertEqual(mirror.currency, "SGD")
        self.assertAlmostEqual(float(mirror.amount), round(1000.00 / 1.28, 2), places=2)
        self.assertAlmostEqual(float(mirror.exchange_rate), 1.28, places=4)
        self.assertAlmostEqual(float(tx_nzd.exchange_rate), round(1.0 / 1.28, 6), places=4)

        # Unlink via PATCH off Transfers, then re-apply all: must re-link existing mirror without duplicate
        unlink_res = self.client.patch(
            f"/transactions/{tx_nzd.id}",
            json={"category": "General"},
        )
        self.assertEqual(unlink_res.status_code, 200)
        self.assertIsNone(unlink_res.json()["linked_transaction_id"])

        reapply_res = self.client.post("/rules/apply-all")
        self.assertEqual(reapply_res.status_code, 200)
        all_after = self.db.scalars(select(Transaction).order_by(Transaction.id.asc())).all()
        self.assertEqual(len(all_after), 2)
        self.db.expire_all()
        self.db.refresh(tx_nzd)
        self.db.refresh(mirror)
        self.assertEqual(tx_nzd.linked_transaction_id, mirror.id)
        self.assertEqual(mirror.linked_transaction_id, tx_nzd.id)


if __name__ == "__main__":
    unittest.main()
