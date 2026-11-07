"""Unit tests for transfer reconciliation, Wise multi-currency conversions, and two-way receipt auto-matching."""

from datetime import date, timedelta
from pathlib import Path
import sys
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base, get_db
from app.main import app
from app.models import Account, Receipt, Transaction
from app.services.receipt_service import reconcile_unlinked_receipts
from app.services.statement_parser import parse_csv_statement
from app.services.transfer_service import match_transfer_pair, reconcile_transfers

TEST_DB_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class TestTransfersAndReceiptLinking(unittest.TestCase):
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

        # Create two test accounts: ASB Everyday Checking (NZD) and Wise Multi-Currency (USD)
        self.acc_asb = Account(
            name="ASB Everyday Checking",
            account_type="checking",
            tier="cash",
            institution="ASB",
            currency="NZD",
        )
        self.acc_wise = Account(
            name="Wise USD Account",
            account_type="savings",
            tier="cash",
            institution="Wise",
            currency="USD",
        )
        self.acc_sharesies = Account(
            name="Sharesies Wallet",
            account_type="brokerage",
            tier="growth",
            institution="Sharesies",
            currency="NZD",
        )
        self.db.add_all([self.acc_asb, self.acc_wise, self.acc_sharesies])
        self.db.commit()
        self.db.refresh(self.acc_asb)
        self.db.refresh(self.acc_wise)
        self.db.refresh(self.acc_sharesies)

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_two_way_receipt_linking_receipt_first_then_transaction(self):
        """Verify unlinked receipt is automatically matched when a transaction arrives later."""
        # Step 1: Create receipt first (no matching transaction yet)
        res_receipt = self.client.post(
            "/receipts",
            json={
                "receipt_date": "2026-11-05",
                "merchant": "Chemist Warehouse Takapuna",
                "total_amount": 42.50,
                "items": [
                    {
                        "item_name": "Optimum Nutrition Creatine 500g",
                        "quantity": 1.0,
                        "unit_price": 42.50,
                        "total_price": 42.50,
                    }
                ],
            },
        )
        self.assertEqual(res_receipt.status_code, 201)
        receipt_data = res_receipt.json()
        self.assertEqual(receipt_data["status"], "unlinked")
        self.assertIsNone(receipt_data["transaction_id"])

        # Step 2: Record matching transaction later via POST /transactions
        res_tx = self.client.post(
            "/transactions",
            json={
                "transaction_date": "2026-11-05",
                "account_id": self.acc_asb.id,
                "amount": -42.50,
                "currency": "NZD",
                "merchant": "Chemist Warehouse",
                "category": "Health",
            },
        )
        self.assertEqual(res_tx.status_code, 201)
        tx_data = res_tx.json()
        self.assertTrue(tx_data["has_receipt"])

        # Step 3: Assert receipt is now matched and linked to the new transaction
        receipt = self.db.get(Receipt, receipt_data["id"])
        self.db.refresh(receipt)
        self.assertEqual(receipt.status, "matched")
        self.assertEqual(receipt.transaction_id, tx_data["id"])

    def test_two_way_receipt_linking_receipt_first_then_quick_log(self):
        """Verify unlinked receipt is automatically matched when transaction is logged via Quick Log."""
        res_receipt = self.client.post(
            "/receipts",
            json={
                "receipt_date": "2026-11-06",
                "merchant": "Pak n Save Albany",
                "total_amount": 128.75,
                "items": [
                    {
                        "item_name": "Weekly Groceries",
                        "quantity": 1.0,
                        "unit_price": 128.75,
                        "total_price": 128.75,
                    }
                ],
            },
        )
        self.assertEqual(res_receipt.status_code, 201)
        receipt_id = res_receipt.json()["id"]

        # Quick log the expense from Shortcuts
        res_quick = self.client.post(
            "/integrations/quick-log",
            json={
                "amount": 128.75,
                "merchant": "Pak n Save",
                "category": "Groceries",
                "transaction_date": "2026-11-06",
            },
        )
        self.assertEqual(res_quick.status_code, 201)
        tx_id = res_quick.json()["id"]

        # Verify receipt is linked
        receipt = self.db.get(Receipt, receipt_id)
        self.db.refresh(receipt)
        self.assertEqual(receipt.status, "matched")
        self.assertEqual(receipt.transaction_id, tx_id)

    def test_same_currency_transfer_reconciliation(self):
        """Verify domestic account transfers (e.g. ASB to Sharesies) pair and exclude from spend."""
        d = date(2026, 11, 4)

        # Leg 1: Outflow from ASB Checking
        res_out = self.client.post(
            "/transactions",
            json={
                "transaction_date": d.isoformat(),
                "account_id": self.acc_asb.id,
                "amount": -500.00,
                "currency": "NZD",
                "merchant": "Transfer to Sharesies",
                "category": "Transfers",
            },
        )
        self.assertEqual(res_out.status_code, 201)
        out_id = res_out.json()["id"]

        # Leg 2: Inflow into Sharesies
        res_in = self.client.post(
            "/transactions",
            json={
                "transaction_date": d.isoformat(),
                "account_id": self.acc_sharesies.id,
                "amount": 500.00,
                "currency": "NZD",
                "merchant": "Direct Credit ASB",
                "category": "General",
            },
        )
        self.assertEqual(res_in.status_code, 201)
        in_id = res_in.json()["id"]

        # Trigger transfer reconciliation endpoint
        res_recon = self.client.post("/transactions/reconcile-transfers")
        self.assertEqual(res_recon.status_code, 200)

        # Inspect both legs in database
        tx_out = self.db.get(Transaction, out_id)
        tx_in = self.db.get(Transaction, in_id)
        self.db.refresh(tx_out)
        self.db.refresh(tx_in)

        self.assertEqual(tx_out.linked_transaction_id, in_id)
        self.assertEqual(tx_in.linked_transaction_id, out_id)
        self.assertEqual(tx_out.category, "Transfers")
        self.assertEqual(tx_in.category, "Transfers")
        self.assertEqual(float(tx_out.exchange_rate), 1.0)
        self.assertEqual(float(tx_in.exchange_rate), 1.0)

    def test_wise_multi_currency_transfer_reconciliation(self):
        """Verify multi-currency transfer (NZD outflow paired with USD inflow via Wise)."""
        d_out = date(2026, 11, 3)
        d_in = date(2026, 11, 4)  # 1 day later

        # NZD Leg: $1,650.00 sent via Wise with $5.00 transfer fee
        res_out = self.client.post(
            "/transactions",
            json={
                "transaction_date": d_out.isoformat(),
                "account_id": self.acc_asb.id,
                "amount": -1650.00,
                "currency": "NZD",
                "merchant": "Wise Transfer NZ",
                "category": "Transfers",
                "fee_amount": 5.00,
            },
        )
        self.assertEqual(res_out.status_code, 201)
        out_id = res_out.json()["id"]

        # USD Leg: $1,000.00 received in Wise USD account
        res_in = self.client.post(
            "/transactions",
            json={
                "transaction_date": d_in.isoformat(),
                "account_id": self.acc_wise.id,
                "amount": 1000.00,
                "currency": "USD",
                "merchant": "Wise Conversion Inflow",
                "category": "General",
            },
        )
        self.assertEqual(res_in.status_code, 201)
        in_id = res_in.json()["id"]

        # Reconcile transfers
        res_recon = self.client.post("/transactions/reconcile-transfers")
        self.assertEqual(res_recon.status_code, 200)

        tx_out = self.db.get(Transaction, out_id)
        tx_in = self.db.get(Transaction, in_id)
        self.db.refresh(tx_out)
        self.db.refresh(tx_in)

        self.assertEqual(tx_out.linked_transaction_id, in_id)
        self.assertEqual(tx_in.linked_transaction_id, out_id)
        # Implied exchange rate: 1000 / 1650 ~ 0.6061
        self.assertAlmostEqual(float(tx_out.exchange_rate), 0.6061, places=3)
        self.assertEqual(tx_in.category, "Transfers")

    def test_wise_csv_statement_parser(self):
        """Verify native parsing of official Wise statement CSV files."""
        sample_wise_csv = """TransferWise ID,Date,Amount,Currency,Description,Payment Reference,Running Balance,Exchange From,Exchange To,Exchange Rate,Payer Name,Payee Name,Payee Account Number,Merchant,Card Last Four Digits,Card Authorisation Code,Total fees
TRANSFER-98765432,04-11-2026,-1000.00,NZD,Transfer to USD account,Balance topup,4500.00,NZD,USD,0.6120,John Doe,John Doe,ACC123,,,,4.50
TRANSFER-98765433,05-11-2026,500.00,NZD,Client Payment Invoice 101,Freelance,5000.00,,,,Client Inc,John Doe,ACC123,,,,0.00
"""
        result = parse_csv_statement(sample_wise_csv, institution="Wise")

        self.assertEqual(result.institution, "Wise")
        self.assertEqual(result.currency, "NZD")
        self.assertEqual(result.closing_balance, 5000.00)
        self.assertEqual(len(result.transactions), 2)

        # Check outflow transaction
        tx1 = result.transactions[0]
        self.assertEqual(tx1.amount, -1000.00)
        self.assertEqual(tx1.currency, "NZD")
        self.assertEqual(tx1.category, "Transfers")
        self.assertEqual(tx1.exchange_rate, 0.6120)
        self.assertEqual(tx1.fee_amount, 4.50)

        # Check inflow transaction
        tx2 = result.transactions[1]
        self.assertEqual(tx2.amount, 500.00)
        self.assertEqual(tx2.category, "Income")
        self.assertEqual(tx2.currency, "NZD")


if __name__ == "__main__":
    unittest.main()
