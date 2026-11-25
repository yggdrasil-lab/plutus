"""Unit tests for canonical transfer_reconciliation primitives and caller integrations."""

from datetime import date
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base
from app.models import Account, CategorizationRule, Transaction
from app.services import rules_engine
from app.services.fx_service import cache_exchange_rate
from app.services.ingestion_service import NormalizedTx, _refresh_existing_btc_transaction
from app.services.transfer_reconciliation import (
    CRYPTO_CURRENCIES,
    TRANSFER_CATEGORIES,
    TRANSFER_KEYWORD_TOKENS,
    TransferCandidate,
    UnlinkTransferResult,
    apply_counterpart_account_for_rule,
    build_reconciliation_candidate_filter,
    compute_implied_crypto_fee,
    fx_rate_precision,
    is_plausible_offline_fx_rate,
    is_transfer_like,
    link_transfer_pair,
    match_transfer_pair,
    reconcile_transfers,
    resolve_unlinked_reporting_rate,
    score_cross_currency_match,
    score_same_currency_match,
    score_transfer_match,
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


class TestTransferScoringAndMatching(unittest.TestCase):
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
        self.acc_php = Account(
            name="Savings PHP",
            account_type="savings",
            tier="cash",
            institution="Bank C",
            currency="PHP",
        )
        self.acc_btc = Account(
            name="Cold Wallet BTC",
            account_type="crypto",
            tier="growth",
            institution="Bitcoin On-Chain",
            currency="BTC",
        )
        self.db.add_all([self.acc_nzd, self.acc_nzd_savings, self.acc_sgd, self.acc_php, self.acc_btc])
        self.db.commit()
        for acc in (self.acc_nzd, self.acc_nzd_savings, self.acc_sgd, self.acc_php, self.acc_btc):
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
        raw_description: str | None = None,
        exchange_rate: float | None = None,
        fee_amount: float | None = None,
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
        )
        self.db.add(tx)
        self.db.flush()
        return tx

    def test_scoring_module_has_zero_sqlalchemy_imports(self) -> None:
        import ast

        scoring_path = (
            Path(__file__).resolve().parent.parent
            / "app"
            / "services"
            / "transfer_reconciliation"
            / "scoring.py"
        )
        tree = ast.parse(scoring_path.read_text(encoding="utf-8"))
        imported_roots: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.extend(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.append(node.module.split(".")[0])
        self.assertNotIn("sqlalchemy", imported_roots)
        self.assertEqual(fx_rate_precision(0.005), 8)
        self.assertEqual(fx_rate_precision(0.5), 6)
        self.assertEqual(fx_rate_precision(1.25), 4)

    def test_score_same_currency_exact_fee_subaccount_and_ties(self) -> None:
        d = date(2026, 11, 10)
        anchor = TransferCandidate(1, 10, d, -200.0, "NZD", "Transfers", "Bank Transfer")
        exact = TransferCandidate(2, 20, d, 200.0, "NZD", "General", "Direct Credit")
        noisy = TransferCandidate(3, 20, d, 200.03, "NZD", "General", "Direct Credit")
        later = TransferCandidate(4, 20, date(2026, 11, 11), 200.0, "NZD", "General", "Direct Credit")
        decision = score_same_currency_match(anchor, [later, noisy, exact])
        self.assertIsNotNone(decision)
        self.assertEqual(decision.partner_id, 2)
        self.assertEqual(decision.score, (0, 0.0))
        self.assertEqual(decision.outflow_exchange_rate, 1.0)
        self.assertEqual(decision.inflow_exchange_rate, 1.0)

        fee_anchor = TransferCandidate(10, 10, d, -205.0, "NZD", "Transfers", "Bank Transfer", fee_amount=5.0)
        fee_decision = score_same_currency_match(fee_anchor, [exact])
        self.assertIsNotNone(fee_decision)
        self.assertEqual(fee_decision.partner_id, 2)
        self.assertIsNone(fee_decision.outflow_fee_amount)

        same_acc_out = TransferCandidate(
            20, 10, d, -85.0, "SGD", "Transfers", "Funds Transfer",
            raw_description="Funds Transfer [Savings (1001)]",
        )
        same_tag_in = TransferCandidate(
            21, 10, d, 85.0, "SGD", "Transfers", "Funds Transfer",
            raw_description="Funds Transfer [Savings (1001)]",
        )
        diff_tag_in = TransferCandidate(
            22, 10, d, 85.0, "SGD", "Transfers", "Funds Transfer",
            raw_description="Funds Transfer [Multiplier (2002)]",
        )
        self.assertIsNone(score_same_currency_match(same_acc_out, [same_tag_in]))
        sub_decision = score_same_currency_match(same_acc_out, [same_tag_in, diff_tag_in])
        self.assertIsNotNone(sub_decision)
        self.assertEqual(sub_decision.partner_id, 22)

    def test_score_transfer_match_prefers_same_currency_over_cross_currency(self) -> None:
        d = date(2026, 11, 10)
        anchor = TransferCandidate(1, 10, d, -200.0, "NZD", "Transfers", "Transfer")
        usd_cand = TransferCandidate(2, 30, d, 120.0, "USD", "General", "Wise USD Credit")
        nzd_cand = TransferCandidate(3, 20, d, 200.0, "NZD", "General", "Transfer In")
        decision = score_transfer_match(
            anchor,
            [usd_cand, nzd_cand],
            lookup_market_rate=lambda _d, _src, _dst: 0.60,
        )
        self.assertIsNotNone(decision)
        self.assertTrue(decision.is_same_currency)
        self.assertEqual(decision.partner_id, 3)

    def test_score_cross_currency_market_explicit_offline_and_crypto_fee(self) -> None:
        d = date(2026, 11, 11)
        sgd_out = TransferCandidate(1, 10, d, -100.0, "SGD", "Transfers", "Wise Remit")
        nzd_close = TransferCandidate(2, 20, d, 128.0, "NZD", "General", "Wise Credit")
        nzd_far = TransferCandidate(3, 20, d, 149.0, "NZD", "General", "Wise Credit")
        nzd_tie_better = TransferCandidate(4, 20, d, 127.95, "NZD", "General", "Wise Credit")
        res = score_cross_currency_match(
            sgd_out,
            [nzd_far, nzd_close, nzd_tie_better],
            lookup_market_rate=lambda _d, _src, _dst: 1.28,
        )
        self.assertIsNotNone(res)
        self.assertEqual(res.partner_id, 2)
        self.assertEqual(res.outflow_exchange_rate, 1.28)
        self.assertIsNone(
            score_cross_currency_match(
                sgd_out,
                [nzd_far],
                lookup_market_rate=lambda _d, _src, _dst: 1.28,
            )
        )

        # Explicit pre-populated NZD outflow rate short-circuits market lookup within 8%, falls back >8%
        explicit_out = TransferCandidate(5, 10, d, -100.0, "SGD", "Transfers", "Wise", exchange_rate=1.30)
        lookup_calls: list[tuple[date, str, str]] = []

        def _spy_lookup(rd: date, src: str, dst: str) -> float:
            lookup_calls.append((rd, src, dst))
            return 1.28

        explicit_hit = score_cross_currency_match(explicit_out, [nzd_close], lookup_market_rate=_spy_lookup)
        self.assertIsNotNone(explicit_hit)
        self.assertEqual(explicit_hit.outflow_exchange_rate, 1.30)
        self.assertEqual(lookup_calls, [])

        bad_explicit_out = TransferCandidate(6, 10, d, -100.0, "SGD", "Transfers", "Wise", exchange_rate=2.00)
        fallback_hit = score_cross_currency_match(bad_explicit_out, [nzd_close], lookup_market_rate=_spy_lookup)
        self.assertIsNotNone(fallback_hit)
        self.assertEqual(len(lookup_calls), 1)

        # Offline plausibility: fiat, high-denom PHP, crypto requires counterparty keyword
        self.assertTrue(is_plausible_offline_fx_rate("SGD", "NZD", 1.28))
        self.assertTrue(is_plausible_offline_fx_rate("SGD", "PHP", 43.5))
        self.assertFalse(is_plausible_offline_fx_rate("NZD", "BTC", 0.000006, counterparty_text="Alex Tan"))
        self.assertTrue(is_plausible_offline_fx_rate("NZD", "BTC", 0.000006, counterparty_text="Lightning Pay"))

        # NZD->crypto quotes 8dp NZD/BTC and compute_implied_crypto_fee enforces bounds
        nzd_btc_out = TransferCandidate(7, 10, d, -5000.0, "NZD", "Transfers", "Lightning Pay")
        btc_in = TransferCandidate(8, 30, d, 0.035, "BTC", "General", "BTC Deposit")
        btc_decision = score_cross_currency_match(
            nzd_btc_out,
            [btc_in],
            lookup_market_rate=lambda _d, _src, _dst: 1.0 / 140000.0,
        )
        self.assertIsNotNone(btc_decision)
        self.assertEqual(btc_decision.outflow_exchange_rate, round(5000.0 / 0.035, 8))
        self.assertEqual(compute_implied_crypto_fee(out_amount=5000.0, in_amount=0.035, market_rate=140000.0), 100.0)
        self.assertIsNone(compute_implied_crypto_fee(out_amount=100.0, in_amount=0.000712, market_rate=140000.0))
        self.assertIsNone(compute_implied_crypto_fee(out_amount=1000.0, in_amount=0.005, market_rate=140000.0))

    def test_unified_keyword_filter_and_is_transfer_like(self) -> None:
        d = date(2026, 11, 13)
        self.assertEqual(len(TRANSFER_KEYWORD_TOKENS), 37)
        self.assertFalse(
            is_transfer_like(category="Transfers", merchant="BASE REWARDS", raw_description=None)
        )
        self.assertFalse(
            is_transfer_like(category="Groceries", merchant="Transfer", raw_description=None)
        )
        for idx, token in enumerate(TRANSFER_KEYWORD_TOKENS):
            with self.subTest(token=token):
                self.assertTrue(
                    is_transfer_like(category="General", merchant=f"Alex {token} test", raw_description=None)
                )
                self.assertTrue(
                    is_transfer_like(category="General", merchant="Alex Tan", raw_description=f"Ref {token}")
                )
                tx_m = self._add_tx(
                    tx_date=d,
                    account_id=self.acc_nzd.id,
                    amount=-10.0,
                    currency="NZD",
                    merchant=f"Pay {token}",
                    raw_hash=f"kw-m-{idx}",
                    category="General",
                    raw_description="Plain ref",
                )
                tx_d = self._add_tx(
                    tx_date=d,
                    account_id=self.acc_nzd.id,
                    amount=-10.0,
                    currency="NZD",
                    merchant="Plain merchant",
                    raw_hash=f"kw-d-{idx}",
                    category="General",
                    raw_description=f"Memo {token}",
                )
                self.db.flush()
                matched_ids = set(
                    self.db.scalars(
                        select(Transaction.id).where(build_reconciliation_candidate_filter())
                    ).all()
                )
                self.assertIn(tx_m.id, matched_ids)
                self.assertIn(tx_d.id, matched_ids)

    def test_end_to_end_match_transfer_pair_with_fake_fetcher(self) -> None:
        d = date(2026, 11, 18)
        fetcher = _FakeFxFetcher(
            {
                (d, "BTC", "NZD"): (140000.0, "fake"),
                (d, "NZD", "BTC"): (1.0 / 140000.0, "fake"),
            }
        )
        cache_exchange_rate(
            self.db, rate_date=d, from_currency="BTC", to_currency="NZD", rate=140000.0, source="fake"
        )
        p3_out = self._add_tx(
            tx_date=d,
            account_id=self.acc_nzd.id,
            amount=-5000.0,
            currency="NZD",
            merchant="Lightning Pay",
            raw_hash="e2e-p3-out",
        )
        p3_in = self._add_tx(
            tx_date=d,
            account_id=self.acc_btc.id,
            amount=0.035,
            currency="BTC",
            merchant="Bitcoin Deposit",
            raw_hash="e2e-p3-in",
            category="General",
        )
        self.db.commit()

        partner = match_transfer_pair(p3_out, self.db, fetcher=fetcher, commit=True)
        self.assertIsNotNone(partner)
        self.assertEqual(partner.id, p3_in.id)
        self.assertAlmostEqual(float(p3_out.fee_amount), 100.0, places=2)
        self.assertAlmostEqual(float(p3_out.exchange_rate), 140000.0, places=2)
        self.assertAlmostEqual(float(p3_in.exchange_rate), 140000.0, places=2)
        self.assertEqual(p3_in.category, "Transfers")

    def test_reconcile_transfers_single_commit_on_autoflush_false(self) -> None:
        d = date(2026, 11, 19)
        fetcher = _FakeFxFetcher({})
        cache_exchange_rate(
            self.db, rate_date=d, from_currency="SGD", to_currency="NZD", rate=1.28, source="fake"
        )
        p1_out = self._add_tx(
            tx_date=d,
            account_id=self.acc_nzd.id,
            amount=-250.0,
            currency="NZD",
            merchant="Transfer to Savings",
            raw_hash="sc-p1-out",
        )
        p1_in = self._add_tx(
            tx_date=d,
            account_id=self.acc_nzd_savings.id,
            amount=250.0,
            currency="NZD",
            merchant="Direct Credit",
            raw_hash="sc-p1-in",
            category="General",
        )
        p2_out = self._add_tx(
            tx_date=d,
            account_id=self.acc_sgd.id,
            amount=-100.0,
            currency="SGD",
            merchant="Wise Transfer",
            raw_hash="sc-p2-out",
        )
        p2_in = self._add_tx(
            tx_date=d,
            account_id=self.acc_nzd.id,
            amount=128.0,
            currency="NZD",
            merchant="Wise Inflow",
            raw_hash="sc-p2-in",
            category="General",
        )
        p3_out = self._add_tx(
            tx_date=d,
            account_id=self.acc_nzd.id,
            amount=-250.0,
            currency="NZD",
            merchant="Transfer to Savings",
            raw_hash="sc-p3-out",
        )
        p3_in = self._add_tx(
            tx_date=d,
            account_id=self.acc_nzd_savings.id,
            amount=250.0,
            currency="NZD",
            merchant="Direct Credit",
            raw_hash="sc-p3-in",
            category="General",
        )
        self.db.commit()

        with patch.object(self.db, "commit", wraps=self.db.commit) as commit_spy:
            matched_count = reconcile_transfers(self.db, fetcher=fetcher)
            self.assertEqual(matched_count, 3)
            self.assertEqual(commit_spy.call_count, 1)
            second_count = reconcile_transfers(self.db, fetcher=fetcher)
            self.assertEqual(second_count, 0)
            self.assertEqual(commit_spy.call_count, 1)

        for tx in (p1_out, p1_in, p2_out, p2_in, p3_out, p3_in):
            self.db.refresh(tx)
        self.assertEqual(p1_out.linked_transaction_id, p1_in.id)
        self.assertEqual(p3_out.linked_transaction_id, p3_in.id)
        self.assertNotEqual(p1_out.linked_transaction_id, p3_in.id)
        self.assertEqual(p2_out.linked_transaction_id, p2_in.id)
        self.assertEqual(p2_in.category, "Transfers")
        self.assertAlmostEqual(float(p2_out.exchange_rate), 1.28, places=4)


if __name__ == "__main__":
    unittest.main()
