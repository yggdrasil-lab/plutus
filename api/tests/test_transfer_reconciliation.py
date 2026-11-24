"""Unit tests for canonical transfer_reconciliation primitives and caller integrations."""

from datetime import date
from pathlib import Path
import sys
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base
from app.models import Account, CategorizationRule, Transaction
from app.services import rules_engine
from app.services.ingestion_service import NormalizedTx, _refresh_existing_btc_transaction
from app.services.transfer_reconciliation import (
    CRYPTO_CURRENCIES,
    TRANSFER_CATEGORIES,
    UnlinkTransferResult,
    apply_counterpart_account_for_rule,
    link_transfer_pair,
    resolve_unlinked_reporting_rate,
    unlink_transfer_pair,
)
from app.services.transfer_service import repair_crypto_transfers_and_rates

TEST_DB_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class _FakeFxFetcher:
    """In-memory FxRateFetcher double keyed by (rate_date, from_currency, to_currency)."""

    def __init__(self, rates: dict[tuple[date, str, str], tuple[float, str]]) -> None:
        self._rates = rates
        self.calls: list[tuple[date, str, str]] = []

    def fetch_rate(
        self,
        *,
        rate_date: date,
        from_currency: str,
        to_currency: str,
    ) -> tuple[float, str] | None:
        key = (rate_date, from_currency.upper(), to_currency.upper())
        self.calls.append(key)
        return self._rates.get(key)


class TestTransferReconciliation(unittest.TestCase):
    def setUp(self) -> None:
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        self.acc_nzd = Account(
            name="Checking NZD",
            account_type="checking",
            tier="cash",
            institution="Bank A",
            currency="NZD",
        )
        self.acc_nzd_savings = Account(
            name="Savings NZD",
            account_type="savings",
            tier="cash",
            institution="Bank A",
            currency="NZD",
        )
        self.acc_sgd = Account(
            name="Checking SGD",
            account_type="checking",
            tier="cash",
            institution="Bank B",
            currency="SGD",
        )
        self.acc_btc = Account(
            name="Cold Wallet BTC",
            account_type="crypto",
            tier="growth",
            institution="Bitcoin On-Chain",
            currency="BTC",
        )
        self.db.add_all([self.acc_nzd, self.acc_nzd_savings, self.acc_sgd, self.acc_btc])
        self.db.commit()
        for acc in (self.acc_nzd, self.acc_nzd_savings, self.acc_sgd, self.acc_btc):
            self.db.refresh(acc)

    def tearDown(self) -> None:
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def _add_tx(
        self,
        *,
        tx_date: date,
        account_id: int,
        amount: float,
        currency: str,
        merchant: str,
        raw_hash: str,
        category: str = "Transfers",
        exchange_rate: float | None = None,
        fee_amount: float | None = None,
        notes: str | None = None,
        raw_description: str | None = None,
    ) -> Transaction:
        tx = Transaction(
            transaction_date=tx_date,
            account_id=account_id,
            amount=amount,
            currency=currency,
            merchant=merchant,
            raw_description=raw_description or merchant,
            category=category,
            raw_hash=raw_hash,
            exchange_rate=exchange_rate,
            fee_amount=fee_amount,
            notes=notes,
        )
        self.db.add(tx)
        self.db.flush()
        return tx

    def test_rules_engine_relocated_constants_absent(self) -> None:
        self.assertFalse(hasattr(rules_engine, "CRYPTO_CURRENCIES"))
        self.assertFalse(hasattr(rules_engine, "TRANSFER_RULE_CATEGORIES"))
        self.assertIn("BTC", CRYPTO_CURRENCIES)
        self.assertEqual(TRANSFER_CATEGORIES, ("transfers", "transfer"))

    def test_link_transfer_pair_guards_and_promotion(self) -> None:
        d = date(2026, 11, 10)
        unsaved_a = Transaction(
            transaction_date=d,
            account_id=self.acc_nzd.id,
            amount=-100.0,
            currency="NZD",
            merchant="Alex Tan",
            category="General",
            raw_hash="unsaved-a",
        )
        saved_b = self._add_tx(
            tx_date=d,
            account_id=self.acc_nzd_savings.id,
            amount=100.0,
            currency="NZD",
            merchant="Alex Tan",
            raw_hash="saved-b",
            category="General",
        )
        with self.assertRaises(ValueError):
            link_transfer_pair(unsaved_a, saved_b)
        with self.assertRaises(ValueError):
            link_transfer_pair(saved_b, saved_b)

        saved_a = self._add_tx(
            tx_date=d,
            account_id=self.acc_nzd.id,
            amount=-100.0,
            currency="NZD",
            merchant="Alex Tan",
            raw_hash="saved-a",
            category="General",
        )
        link_transfer_pair(saved_a, saved_b, promote_general_category=False)
        self.assertEqual(saved_a.linked_transaction_id, saved_b.id)
        self.assertEqual(saved_b.linked_transaction_id, saved_a.id)
        self.assertEqual(saved_a.category, "General")
        self.assertEqual(saved_b.category, "General")

        link_transfer_pair(saved_a, saved_b, promote_general_category=True)
        self.assertEqual(saved_a.category, "Transfers")
        self.assertEqual(saved_b.category, "Transfers")

    def test_unlink_transfer_pair_across_currency_pairs_and_edge_cases(self) -> None:
        d = date(2026, 11, 12)
        fetcher = _FakeFxFetcher(
            {
                (d, "SGD", "NZD"): (1.284567891, "fake"),
                (d, "BTC", "NZD"): (165432.123456789, "fake"),
            }
        )

        # 1. NZD <-> NZD: clears links, fee_amount, and sets exchange_rate=None
        nzd_out = self._add_tx(
            tx_date=d,
            account_id=self.acc_nzd.id,
            amount=-250.0,
            currency="NZD",
            merchant="Transfer Out",
            raw_hash="nzd-nzd-out",
            exchange_rate=1.0,
            fee_amount=2.5,
        )
        nzd_in = self._add_tx(
            tx_date=d,
            account_id=self.acc_nzd_savings.id,
            amount=247.5,
            currency="NZD",
            merchant="Transfer In",
            raw_hash="nzd-nzd-in",
            exchange_rate=1.0,
            fee_amount=1.0,
        )
        link_transfer_pair(nzd_out, nzd_in)
        res_nzd = unlink_transfer_pair(self.db, nzd_out, fetcher=fetcher)
        self.assertEqual(res_nzd, UnlinkTransferResult(unlinked=True, partner_id=nzd_in.id))
        self.assertIsNone(nzd_out.linked_transaction_id)
        self.assertIsNone(nzd_in.linked_transaction_id)
        self.assertIsNone(nzd_out.fee_amount)
        self.assertIsNone(nzd_in.fee_amount)
        self.assertIsNone(nzd_out.exchange_rate)
        self.assertIsNone(nzd_in.exchange_rate)

        # 2. NZD <-> SGD: clears fees, NZD -> None, SGD -> 8dp SGD->NZD via fetcher
        nzd_leg = self._add_tx(
            tx_date=d,
            account_id=self.acc_nzd.id,
            amount=-500.0,
            currency="NZD",
            merchant="Remit SGD",
            raw_hash="nzd-sgd-out",
            exchange_rate=0.77,
            fee_amount=5.0,
        )
        sgd_leg = self._add_tx(
            tx_date=d,
            account_id=self.acc_sgd.id,
            amount=385.0,
            currency="SGD",
            merchant="Remit In",
            raw_hash="nzd-sgd-in",
            exchange_rate=1.0,
            fee_amount=3.0,
        )
        link_transfer_pair(nzd_leg, sgd_leg)
        res_sgd = unlink_transfer_pair(self.db, nzd_leg, fetcher=fetcher)
        self.assertEqual(res_sgd, UnlinkTransferResult(unlinked=True, partner_id=sgd_leg.id))
        self.assertIsNone(nzd_leg.fee_amount)
        self.assertIsNone(sgd_leg.fee_amount)
        self.assertIsNone(nzd_leg.exchange_rate)
        self.assertEqual(float(sgd_leg.exchange_rate), round(1.284567891, 8))

        # 3. NZD <-> BTC: clears fees, NZD -> None, BTC -> 8dp BTC->NZD via fetcher
        nzd_btc_leg = self._add_tx(
            tx_date=d,
            account_id=self.acc_nzd.id,
            amount=-1000.0,
            currency="NZD",
            merchant="Crypto Buy",
            raw_hash="nzd-btc-out",
            exchange_rate=165000.0,
            fee_amount=15.0,
        )
        btc_leg = self._add_tx(
            tx_date=d,
            account_id=self.acc_btc.id,
            amount=0.006,
            currency="BTC",
            merchant="BTC Deposit",
            raw_hash="nzd-btc-in",
            exchange_rate=160000.0,
            fee_amount=0.00001,
        )
        link_transfer_pair(nzd_btc_leg, btc_leg)
        res_btc = unlink_transfer_pair(self.db, btc_leg, fetcher=fetcher)
        self.assertEqual(res_btc, UnlinkTransferResult(unlinked=True, partner_id=nzd_btc_leg.id))
        self.assertIsNone(nzd_btc_leg.fee_amount)
        self.assertIsNone(btc_leg.fee_amount)
        self.assertIsNone(nzd_btc_leg.exchange_rate)
        self.assertEqual(float(btc_leg.exchange_rate), round(165432.123456789, 8))

        # 4. Dangling partner ID: clears self and returns unlinked=True, partner_id=None
        dangling = self._add_tx(
            tx_date=d,
            account_id=self.acc_sgd.id,
            amount=-50.0,
            currency="SGD",
            merchant="Dangling",
            raw_hash="dangling-sgd",
            exchange_rate=1.0,
            fee_amount=1.25,
        )
        dangling.linked_transaction_id = 999999
        res_dangling = unlink_transfer_pair(self.db, dangling, fetcher=fetcher)
        self.assertEqual(res_dangling, UnlinkTransferResult(unlinked=True, partner_id=None))
        self.assertIsNone(dangling.linked_transaction_id)
        self.assertIsNone(dangling.fee_amount)
        self.assertEqual(float(dangling.exchange_rate), round(1.284567891, 8))

        # 5. Already unlinked row with restore_self_when_unlinked False vs True
        unlinked_skip = self._add_tx(
            tx_date=d,
            account_id=self.acc_sgd.id,
            amount=-30.0,
            currency="SGD",
            merchant="Already Unlinked",
            raw_hash="unlinked-skip",
            exchange_rate=1.11,
            fee_amount=2.0,
        )
        res_skip = unlink_transfer_pair(
            self.db, unlinked_skip, fetcher=fetcher, restore_self_when_unlinked=False
        )
        self.assertEqual(res_skip, UnlinkTransferResult(unlinked=False, partner_id=None))
        self.assertEqual(float(unlinked_skip.exchange_rate), 1.11)
        self.assertEqual(float(unlinked_skip.fee_amount), 2.0)

        res_restore = unlink_transfer_pair(
            self.db, unlinked_skip, fetcher=fetcher, restore_self_when_unlinked=True
        )
        self.assertEqual(res_restore, UnlinkTransferResult(unlinked=False, partner_id=None))
        self.assertIsNone(unlinked_skip.fee_amount)
        self.assertEqual(float(unlinked_skip.exchange_rate), round(1.284567891, 8))
        self.assertIsNone(resolve_unlinked_reporting_rate(self.db, nzd_out, fetcher=fetcher))

    def test_apply_counterpart_account_for_rule_lifecycle(self) -> None:
        d = date(2026, 11, 14)
        fetcher = _FakeFxFetcher(
            {
                (d, "NZD", "SGD"): (0.78, "fake"),
                (d, "SGD", "NZD"): (1.28205128, "fake"),
                (d, "NZD", "BTC"): (0.00000605, "fake"),
                (d, "BTC", "NZD"): (165289.25619835, "fake"),
            }
        )

        # 1. Non-transfer rule sets counterpart_account_id without mirroring
        non_transfer_rule = CategorizationRule(
            name="Groceries Rule",
            description_pattern="MARKET",
            target_category="Groceries",
            counterpart_account_id=self.acc_nzd_savings.id,
        )
        tx_groceries = self._add_tx(
            tx_date=d,
            account_id=self.acc_nzd.id,
            amount=-45.0,
            currency="NZD",
            merchant="Corner Market",
            raw_hash="rule-groceries",
            category="Groceries",
        )
        self.db.add(non_transfer_rule)
        self.db.commit()
        self.assertTrue(
            apply_counterpart_account_for_rule(self.db, tx_groceries, non_transfer_rule, fetcher=fetcher)
        )
        self.assertEqual(tx_groceries.counterpart_account_id, self.acc_nzd_savings.id)
        self.assertIsNone(tx_groceries.linked_transaction_id)

        # 2. Same-currency mirror creation, idempotency, and relink after unlink
        same_rule = CategorizationRule(
            name="NZD Sweep",
            description_pattern="SWEEP",
            target_category="Transfers",
            counterpart_account_id=self.acc_nzd_savings.id,
        )
        tx_same = self._add_tx(
            tx_date=d,
            account_id=self.acc_nzd.id,
            amount=-300.0,
            currency="NZD",
            merchant="Auto Sweep",
            raw_hash="rule-same-nzd",
        )
        self.db.add(same_rule)
        self.db.commit()
        self.assertTrue(apply_counterpart_account_for_rule(self.db, tx_same, same_rule, fetcher=fetcher))
        mirror_same = self.db.get(Transaction, tx_same.linked_transaction_id)
        self.assertIsNotNone(mirror_same)
        self.assertEqual(float(mirror_same.amount), 300.0)
        self.assertEqual(float(tx_same.exchange_rate), 1.0)
        self.assertEqual(float(mirror_same.exchange_rate), 1.0)
        self.assertFalse(apply_counterpart_account_for_rule(self.db, tx_same, same_rule, fetcher=fetcher))

        unlink_transfer_pair(self.db, tx_same, fetcher=fetcher)
        self.assertIsNone(tx_same.linked_transaction_id)
        self.assertTrue(apply_counterpart_account_for_rule(self.db, tx_same, same_rule, fetcher=fetcher))
        self.assertEqual(tx_same.linked_transaction_id, mirror_same.id)
        self.assertEqual(mirror_same.linked_transaction_id, tx_same.id)

        # 3. Cross-currency outflow (NZD -> SGD)
        outflow_rule = CategorizationRule(
            name="SGD Topup",
            description_pattern="SGD TOPUP",
            target_category="Transfers",
            counterpart_account_id=self.acc_sgd.id,
        )
        tx_outflow = self._add_tx(
            tx_date=d,
            account_id=self.acc_nzd.id,
            amount=-1000.0,
            currency="NZD",
            merchant="SGD Topup",
            raw_hash="rule-out-sgd",
        )
        self.db.add(outflow_rule)
        self.db.commit()
        self.assertTrue(
            apply_counterpart_account_for_rule(self.db, tx_outflow, outflow_rule, fetcher=fetcher)
        )
        mirror_out = self.db.get(Transaction, tx_outflow.linked_transaction_id)
        self.assertEqual(mirror_out.currency, "SGD")
        self.assertEqual(float(mirror_out.amount), 780.0)
        self.assertEqual(float(tx_outflow.exchange_rate), 0.78)
        self.assertEqual(float(mirror_out.exchange_rate), 1.28205128)

        # 4. Cross-currency inflow (NZD <- SGD)
        inflow_rule = CategorizationRule(
            name="From SGD",
            description_pattern="FROM SGD",
            target_category="Transfers",
            counterpart_account_id=self.acc_sgd.id,
        )
        tx_inflow = self._add_tx(
            tx_date=d,
            account_id=self.acc_nzd.id,
            amount=500.0,
            currency="NZD",
            merchant="From SGD",
            raw_hash="rule-in-sgd",
        )
        self.db.add(inflow_rule)
        self.db.commit()
        self.assertTrue(
            apply_counterpart_account_for_rule(self.db, tx_inflow, inflow_rule, fetcher=fetcher)
        )
        mirror_in = self.db.get(Transaction, tx_inflow.linked_transaction_id)
        self.assertEqual(mirror_in.currency, "SGD")
        self.assertEqual(float(mirror_in.amount), -390.0)
        self.assertEqual(float(tx_inflow.exchange_rate), 1.0)
        self.assertAlmostEqual(float(mirror_in.exchange_rate), round(1.0 / 0.78, 4), places=4)

        # 5. Cross-currency crypto mirror precision (8dp amount & 8dp small rate)
        btc_rule = CategorizationRule(
            name="BTC Auto",
            description_pattern="BTC AUTO",
            target_category="Transfers",
            counterpart_account_id=self.acc_btc.id,
        )
        tx_btc_buy = self._add_tx(
            tx_date=d,
            account_id=self.acc_nzd.id,
            amount=-1000.0,
            currency="NZD",
            merchant="BTC Auto",
            raw_hash="rule-out-btc",
        )
        self.db.add(btc_rule)
        self.db.commit()
        self.assertTrue(
            apply_counterpart_account_for_rule(self.db, tx_btc_buy, btc_rule, fetcher=fetcher)
        )
        mirror_btc = self.db.get(Transaction, tx_btc_buy.linked_transaction_id)
        self.assertEqual(mirror_btc.currency, "BTC")
        self.assertEqual(float(mirror_btc.amount), 0.00605)
        self.assertEqual(float(tx_btc_buy.exchange_rate), 0.00000605)
        self.assertEqual(float(mirror_btc.exchange_rate), 165289.25619835)

    def test_repair_crypto_transfers_unlinks_mismatched_nzd_and_sgd_legs(self) -> None:
        d = date(2026, 11, 15)
        fetcher = _FakeFxFetcher(
            {
                (d, "BTC", "NZD"): (160000.0, "fake"),
                (d, "NZD", "BTC"): (1.0 / 160000.0, "fake"),
                (d, "SGD", "NZD"): (1.275, "fake"),
                (d, "SGD", "BTC"): (1.275 / 160000.0, "fake"),
            }
        )

        # Mismatched NZD <-> BTC (implied 1000 NZD -> 0.01 BTC = 100k vs 160k market)
        bad_nzd = self._add_tx(
            tx_date=d,
            account_id=self.acc_nzd.id,
            amount=-1000.0,
            currency="NZD",
            merchant="Alex Tan Transfer",
            raw_hash="bad-nzd",
            fee_amount=10.0,
            exchange_rate=100000.0,
        )
        bad_btc_a = self._add_tx(
            tx_date=d,
            account_id=self.acc_btc.id,
            amount=0.01,
            currency="BTC",
            merchant="BTC Deposit",
            raw_hash="bad-btc-a",
            fee_amount=0.0001,
            exchange_rate=100000.0,
        )
        link_transfer_pair(bad_nzd, bad_btc_a)

        # Mismatched SGD <-> BTC
        bad_sgd = self._add_tx(
            tx_date=d,
            account_id=self.acc_sgd.id,
            amount=-1000.0,
            currency="SGD",
            merchant="Alex Tan Transfer",
            raw_hash="bad-sgd",
            fee_amount=12.0,
            exchange_rate=1.0,
        )
        bad_btc_b = self._add_tx(
            tx_date=d,
            account_id=self.acc_btc.id,
            amount=0.02,
            currency="BTC",
            merchant="BTC Deposit",
            raw_hash="bad-btc-b",
            fee_amount=0.0002,
            exchange_rate=160000.0,
        )
        link_transfer_pair(bad_sgd, bad_btc_b)
        self.db.commit()

        repaired = repair_crypto_transfers_and_rates(self.db, fetcher=fetcher)
        self.assertGreater(repaired, 0)
        for row in (bad_nzd, bad_btc_a, bad_sgd, bad_btc_b):
            self.db.refresh(row)

        self.assertIsNone(bad_nzd.linked_transaction_id)
        self.assertIsNone(bad_btc_a.linked_transaction_id)
        self.assertIsNone(bad_nzd.fee_amount)
        self.assertIsNone(bad_btc_a.fee_amount)
        self.assertIsNone(bad_nzd.exchange_rate)
        self.assertEqual(float(bad_btc_a.exchange_rate), 160000.0)

        self.assertIsNone(bad_sgd.linked_transaction_id)
        self.assertIsNone(bad_btc_b.linked_transaction_id)
        self.assertIsNone(bad_sgd.fee_amount)
        self.assertIsNone(bad_btc_b.fee_amount)
        self.assertEqual(float(bad_sgd.exchange_rate), 1.275)
        self.assertEqual(float(bad_btc_b.exchange_rate), 160000.0)

    def test_refresh_existing_btc_transaction_unlinks_and_restores_rates_on_date_change(self) -> None:
        old_date = date(2026, 11, 16)
        new_date = date(2026, 11, 17)
        fetcher = _FakeFxFetcher(
            {
                (old_date, "SGD", "NZD"): (1.295, "fake"),
                (new_date, "BTC", "NZD"): (171234.5, "fake"),
            }
        )

        # Case A: BTC linked to NZD partner
        nzd_partner = self._add_tx(
            tx_date=old_date,
            account_id=self.acc_nzd.id,
            amount=-500.0,
            currency="NZD",
            merchant="Crypto Buy",
            raw_hash="refresh-nzd-partner",
            fee_amount=5.0,
            exchange_rate=168000.0,
        )
        btc_a = self._add_tx(
            tx_date=old_date,
            account_id=self.acc_btc.id,
            amount=0.003,
            currency="BTC",
            merchant="BTC Deposit",
            raw_hash="refresh-btc-a",
            fee_amount=0.00001,
            exchange_rate=168000.0,
            notes="btc:" + ("a" * 64),
        )
        link_transfer_pair(nzd_partner, btc_a)

        # Case B: BTC linked to SGD partner
        sgd_partner = self._add_tx(
            tx_date=old_date,
            account_id=self.acc_sgd.id,
            amount=-400.0,
            currency="SGD",
            merchant="SGD Crypto",
            raw_hash="refresh-sgd-partner",
            fee_amount=4.0,
            exchange_rate=1.0,
        )
        btc_b = self._add_tx(
            tx_date=old_date,
            account_id=self.acc_btc.id,
            amount=0.0031,
            currency="BTC",
            merchant="BTC Deposit",
            raw_hash="refresh-btc-b",
            fee_amount=0.00002,
            exchange_rate=168000.0,
            notes="btc:" + ("b" * 64),
        )
        link_transfer_pair(sgd_partner, btc_b)
        self.db.commit()

        item_a = NormalizedTx(
            transaction_date=new_date,
            amount=0.003,
            currency="BTC",
            merchant="BTC Deposit",
            category="Transfers",
            notes=btc_a.notes,
        )
        item_b = NormalizedTx(
            transaction_date=new_date,
            amount=0.0031,
            currency="BTC",
            merchant="BTC Deposit",
            category="Transfers",
            notes=btc_b.notes,
        )

        self.assertTrue(
            _refresh_existing_btc_transaction(self.db, self.acc_btc, item_a, btc_a, fetcher=fetcher)
        )
        self.assertTrue(
            _refresh_existing_btc_transaction(self.db, self.acc_btc, item_b, btc_b, fetcher=fetcher)
        )

        self.assertEqual(btc_a.transaction_date, new_date)
        self.assertIsNone(btc_a.linked_transaction_id)
        self.assertIsNone(nzd_partner.linked_transaction_id)
        self.assertIsNone(btc_a.fee_amount)
        self.assertIsNone(nzd_partner.fee_amount)
        self.assertIsNone(nzd_partner.exchange_rate)
        self.assertEqual(float(btc_a.exchange_rate), 171234.5)

        self.assertEqual(btc_b.transaction_date, new_date)
        self.assertIsNone(btc_b.linked_transaction_id)
        self.assertIsNone(sgd_partner.linked_transaction_id)
        self.assertIsNone(btc_b.fee_amount)
        self.assertIsNone(sgd_partner.fee_amount)
        self.assertEqual(float(sgd_partner.exchange_rate), 1.295)
        self.assertEqual(float(btc_b.exchange_rate), 171234.5)


if __name__ == "__main__":
    unittest.main()
