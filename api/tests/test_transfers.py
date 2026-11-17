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
        self.acc_dbs = Account(
            name="DBS Singapore Checking",
            account_type="checking",
            tier="cash",
            institution="DBS Bank",
            currency="SGD",
        )
        self.acc_bpi = Account(
            name="BPI Manila Savings",
            account_type="savings",
            tier="cash",
            institution="BPI",
            currency="PHP",
        )
        self.acc_btc = Account(
            name="Bitcoin Hardware Vault",
            account_type="brokerage",
            tier="growth",
            institution="Crypto",
            currency="BTC",
        )
        self.db.add_all([self.acc_asb, self.acc_wise, self.acc_sharesies, self.acc_dbs, self.acc_bpi, self.acc_btc])
        self.db.commit()
        self.db.refresh(self.acc_asb)
        self.db.refresh(self.acc_wise)
        self.db.refresh(self.acc_sharesies)
        self.db.refresh(self.acc_dbs)
        self.db.refresh(self.acc_bpi)
        self.db.refresh(self.acc_btc)

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
        # Implied exchange rate: 1000 / (1650 - 5) = 1000 / 1645 ~ 0.6079
        self.assertAlmostEqual(float(tx_out.exchange_rate), 0.6079, places=3)
        self.assertEqual(tx_in.category, "Transfers")

    def test_dbs_singapore_to_philippine_bpi_remittance(self):
        """Verify cross-border remittance from DBS Singapore (SGD) to Philippine BPI (PHP)."""
        d_out = date(2026, 11, 2)
        d_in = date(2026, 11, 3)

        # SGD Outflow: 1,000 SGD with 5.00 SGD remittance fee
        res_out = self.client.post(
            "/transactions",
            json={
                "transaction_date": d_out.isoformat(),
                "account_id": self.acc_dbs.id,
                "amount": -1000.00,
                "currency": "SGD",
                "merchant": "DBS Remit to BPI Manila",
                "category": "Transfers",
                "fee_amount": 5.00,
            },
        )
        self.assertEqual(res_out.status_code, 201)
        out_id = res_out.json()["id"]

        # PHP Inflow: 43,500 PHP received
        res_in = self.client.post(
            "/transactions",
            json={
                "transaction_date": d_in.isoformat(),
                "account_id": self.acc_bpi.id,
                "amount": 43500.00,
                "currency": "PHP",
                "merchant": "Remittance Credit DBS",
                "category": "General",
            },
        )
        self.assertEqual(res_in.status_code, 201)
        in_id = res_in.json()["id"]

        res_recon = self.client.post("/transactions/reconcile-transfers")
        self.assertEqual(res_recon.status_code, 200)

        tx_out = self.db.get(Transaction, out_id)
        tx_in = self.db.get(Transaction, in_id)
        self.db.refresh(tx_out)
        self.db.refresh(tx_in)

        self.assertEqual(tx_out.linked_transaction_id, in_id)
        self.assertEqual(tx_in.linked_transaction_id, out_id)
        # Implied rate: 43500 / (1000 - 5) = 43500 / 995 ~ 43.7186 PHP/SGD
        self.assertAlmostEqual(float(tx_out.exchange_rate), 43.7186, places=2)
        self.assertEqual(tx_in.category, "Transfers")

    def test_bitcoin_crypto_conversion_transfer(self):
        """Verify fiat-to-crypto purchase transfer (NZD outflow paired with BTC inflow)."""
        d = date(2026, 11, 4)

        # NZD Outflow: $5,000 NZD sent to crypto platform
        res_out = self.client.post(
            "/transactions",
            json={
                "transaction_date": d.isoformat(),
                "account_id": self.acc_asb.id,
                "amount": -5000.00,
                "currency": "NZD",
                "merchant": "EasyCrypto Bitcoin Purchase",
                "category": "Transfers",
            },
        )
        self.assertEqual(res_out.status_code, 201)
        out_id = res_out.json()["id"]

        # BTC Inflow: 0.035 BTC received in hardware vault
        res_in = self.client.post(
            "/transactions",
            json={
                "transaction_date": d.isoformat(),
                "account_id": self.acc_btc.id,
                "amount": 0.035,
                "currency": "BTC",
                "merchant": "Bitcoin Inflow Network",
                "category": "General",
            },
        )
        self.assertEqual(res_in.status_code, 201)
        in_id = res_in.json()["id"]

        res_recon = self.client.post("/transactions/reconcile-transfers")
        self.assertEqual(res_recon.status_code, 200)

        tx_out = self.db.get(Transaction, out_id)
        tx_in = self.db.get(Transaction, in_id)
        self.db.refresh(tx_out)
        self.db.refresh(tx_in)

        self.assertEqual(tx_out.linked_transaction_id, in_id)
        self.assertEqual(tx_in.linked_transaction_id, out_id)
        # Implied rate: 0.035 / 5000 = 0.000007 BTC/NZD
        self.assertAlmostEqual(float(tx_out.exchange_rate), 0.000007, places=6)
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

    def test_same_currency_preferred_over_foreign_currency_candidate(self):
        """Verify NZD-to-NZD transfer pairs with exact NZD leg instead of a nearby USD candidate."""
        d = date(2026, 11, 10)

        # Unrelated foreign-currency inflow on Wise USD within 4-day window
        wise_tx = Transaction(
            transaction_date=d,
            account_id=self.acc_wise.id,
            amount=120.00,
            currency="USD",
            merchant="Wise USD Credit",
            category="General",
            raw_hash="wise_usd_distractor_1",
        )
        # Same-currency ANZ/ASB outflow & inflow
        nzd_out = Transaction(
            transaction_date=d,
            account_id=self.acc_asb.id,
            amount=-200.00,
            currency="NZD",
            merchant="Transfer to Savings",
            category="Transfers",
            raw_hash="nzd_out_same_curr_1",
        )
        nzd_in = Transaction(
            transaction_date=d,
            account_id=self.acc_sharesies.id,
            amount=200.00,
            currency="NZD",
            merchant="Transfer from Checking",
            category="General",
            raw_hash="nzd_in_same_curr_1",
        )
        self.db.add_all([wise_tx, nzd_out, nzd_in])
        self.db.commit()
        self.db.refresh(wise_tx)
        self.db.refresh(nzd_out)
        self.db.refresh(nzd_in)

        matched = match_transfer_pair(nzd_out, self.db)
        self.assertIsNotNone(matched)
        self.assertEqual(matched.id, nzd_in.id)
        self.db.refresh(nzd_out)
        self.db.refresh(nzd_in)
        self.assertEqual(float(nzd_out.exchange_rate), 1.0)
        self.assertEqual(float(nzd_in.exchange_rate), 1.0)

    def test_recategorize_transfer_to_expense_clears_link_and_fx_rate(self):
        """Verify changing a linked transfer to an expense category (e.g. Gym) clears FX rate and unlinks both legs."""
        d = date(2026, 11, 12)
        out_tx = Transaction(
            transaction_date=d,
            account_id=self.acc_asb.id,
            amount=-35.00,
            currency="NZD",
            merchant="Transfer Badminton Club",
            category="Transfers",
            raw_hash="badminton_out_1",
            exchange_rate=0.612,
            fee_amount=1.50,
        )
        in_tx = Transaction(
            transaction_date=d,
            account_id=self.acc_wise.id,
            amount=21.42,
            currency="USD",
            merchant="Wise Receipt",
            category="Transfers",
            raw_hash="badminton_in_1",
            exchange_rate=0.612,
        )
        self.db.add_all([out_tx, in_tx])
        self.db.commit()
        self.db.refresh(out_tx)
        self.db.refresh(in_tx)

        out_tx.linked_transaction_id = in_tx.id
        in_tx.linked_transaction_id = out_tx.id
        self.db.commit()

        res = self.client.patch(
            f"/transactions/{out_tx.id}",
            json={"category": "Gym"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["category"], "Gym")
        self.assertIsNone(data["linked_transaction_id"])
        self.assertIsNone(data["exchange_rate"])
        self.assertIsNone(data["fee_amount"])

        self.db.expire_all()
        self.db.refresh(in_tx)
        self.assertIsNone(in_tx.linked_transaction_id)
        self.assertIsNone(in_tx.exchange_rate)
        self.assertIsNone(in_tx.fee_amount)

        # Subsequent reconciliation must not re-link the Gym transaction
        reconcile_transfers(self.db)
        self.db.refresh(out_tx)
        self.assertIsNone(out_tx.linked_transaction_id)
        self.assertIsNone(out_tx.exchange_rate)

    def test_does_not_link_transfer_to_unrelated_general_purchase(self):
        wise_sgd = Account(
            name="Wise SGD",
            institution="Wise",
            account_type="checking",
            tier="cash",
            currency="SGD",
        )
        wise_nzd = Account(
            name="Wise Checking",
            institution="Wise",
            account_type="checking",
            tier="cash",
            currency="NZD",
        )
        self.db.add_all([wise_sgd, wise_nzd])
        self.db.commit()
        self.db.refresh(wise_sgd)
        self.db.refresh(wise_nzd)

        deposit = Transaction(
            transaction_date=date(2026, 11, 6),
            account_id=wise_sgd.id,
            amount=2000.0,
            currency="SGD",
            merchant="TIENZO RAYMUND MAGLALANG",
            category="Transfers",
            raw_hash="sgd-deposit-2000",
        )
        amazon = Transaction(
            transaction_date=date(2026, 11, 2),
            account_id=wise_nzd.id,
            amount=-86.14,
            currency="NZD",
            merchant="Amazon Marketplace Au",
            category="General",
            raw_hash="nzd-amazon-86",
        )
        self.db.add_all([deposit, amazon])
        self.db.commit()

        matched = match_transfer_pair(deposit, self.db)
        self.assertIsNone(matched)
        self.db.refresh(deposit)
        self.db.refresh(amazon)
        self.assertIsNone(deposit.linked_transaction_id)
        self.assertIsNone(amazon.linked_transaction_id)

    def test_rejects_implausible_fx_rate_and_bank_noise_when_offline(self):
        from unittest.mock import patch

        asb_nzd = Account(
            name="ANZ Freedom",
            institution="ANZ",
            account_type="checking",
            tier="cash",
            currency="NZD",
        )
        dbs_sgd = Account(
            name="DBS Savings",
            institution="DBS Bank",
            account_type="savings",
            tier="cash",
            currency="SGD",
        )
        self.db.add_all([asb_nzd, dbs_sgd])
        self.db.commit()
        self.db.refresh(asb_nzd)
        self.db.refresh(dbs_sgd)

        nzd_out = Transaction(
            transaction_date=date(2026, 10, 12),
            account_id=asb_nzd.id,
            amount=-1990.66,
            currency="NZD",
            merchant="Credit Card payment",
            category="Transfers",
            raw_hash="nzd-cc-1990",
        )
        sgd_giro = Transaction(
            transaction_date=date(2026, 10, 14),
            account_id=dbs_sgd.id,
            amount=65.54,
            currency="SGD",
            merchant="GIRO Payments / Collections via GIRO",
            category="Transfers",
            raw_hash="sgd-giro-65",
        )
        sgd_reward = Transaction(
            transaction_date=date(2026, 10, 13),
            account_id=dbs_sgd.id,
            amount=0.13,
            currency="SGD",
            merchant="BASE REWARDS",
            category="Transfers",
            raw_hash="sgd-reward-013",
        )
        self.db.add_all([nzd_out, sgd_giro, sgd_reward])
        self.db.commit()

        with patch("app.services.transfer_service.get_exchange_rate", return_value=None):
            matched = match_transfer_pair(nzd_out, self.db)

        self.assertIsNone(matched)
        self.db.refresh(nzd_out)
        self.db.refresh(sgd_giro)
        self.db.refresh(sgd_reward)
        self.assertIsNone(nzd_out.linked_transaction_id)
        self.assertIsNone(nzd_out.exchange_rate)
        self.assertIsNone(sgd_giro.linked_transaction_id)
        self.assertIsNone(sgd_reward.linked_transaction_id)


if __name__ == "__main__":
    unittest.main()
