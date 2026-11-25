"""Unit tests for deep fx_repair, SQL filtering, and ingestion FX fetcher threading."""

from datetime import date, timedelta
from pathlib import Path
import sys
import unittest

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base
from app.models import Account, Transaction
from app.services import burn_service
from app.services.fx_repair import (
    repair_crypto_transfers_and_rates,
    repair_transaction_fx_rates,
    run_startup_fx_repairs,
)
from app.services.ingestion_service import (
    NormalizedTx,
    ingest_normalized_transactions,
    upsert_account_snapshot,
)
from app.services.transfer_reconciliation import (
    CRYPTO_COUNTERPARTY_KEYWORD_TOKENS,
    TRANSFER_KEYWORD_TOKENS,
    build_reconciliation_candidate_filter,
    link_transfer_pair,
)


class _RecordingFxFetcher:
    """In-memory FX fetcher asserting zero external HTTP calls."""

    def __init__(self, mapping: dict[tuple[date, str, str], tuple[float, str]]) -> None:
        self.mapping = mapping
        self.calls: list[tuple[date, str, str]] = []

    def fetch_rate(self, *, rate_date: date, from_currency: str, to_currency: str):
        key = (rate_date, from_currency.upper(), to_currency.upper())
        self.calls.append(key)
        return self.mapping.get(key)


class TestFxRepairAndThreading(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        Base.metadata.create_all(bind=self.engine)
        self.db = self.Session()

        self.acc_nzd = Account(
            name="ASB Checking",
            institution="ASB",
            account_type="checking",
            tier="cash",
            currency="NZD",
        )
        self.acc_sgd = Account(
            name="DBS Multiplier",
            institution="DBS Bank",
            account_type="savings",
            tier="cash",
            currency="SGD",
        )
        self.acc_btc = Account(
            name="Hardware Wallet",
            institution="Bitcoin On-Chain",
            account_type="crypto",
            tier="growth",
            currency="BTC",
        )
        self.db.add_all([self.acc_nzd, self.acc_sgd, self.acc_btc])
        self.db.commit()
        for acc in (self.acc_nzd, self.acc_sgd, self.acc_btc):
            self.db.refresh(acc)

    def tearDown(self) -> None:
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_burn_service_no_longer_defines_repair_transaction_fx_rates(self) -> None:
        self.assertFalse(hasattr(burn_service, "repair_transaction_fx_rates"))

    def test_transfer_keywords_compose_crypto_tokens_without_duplicates(self) -> None:
        self.assertEqual(len(TRANSFER_KEYWORD_TOKENS), 37)
        self.assertEqual(len(set(TRANSFER_KEYWORD_TOKENS)), 37)
        for token in CRYPTO_COUNTERPARTY_KEYWORD_TOKENS:
            self.assertIn(token, TRANSFER_KEYWORD_TOKENS)
        self.assertIsNotNone(build_reconciliation_candidate_filter())

    def test_repair_crypto_transfers_and_rates_uses_sql_currency_where_filter(self) -> None:
        d = date(2026, 11, 10)
        nzd_tx = Transaction(
            transaction_date=d,
            account_id=self.acc_nzd.id,
            amount=-25.0,
            currency="NZD",
            merchant="Bakery",
            category="Food",
            raw_hash="sql-filter-nzd",
        )
        btc_tx = Transaction(
            transaction_date=d,
            account_id=self.acc_btc.id,
            amount=0.01,
            currency="BTC",
            merchant="Bitcoin Deposit",
            category="Transfers",
            exchange_rate=None,
            raw_hash="sql-filter-btc",
        )
        self.db.add_all([nzd_tx, btc_tx])
        self.db.commit()

        executed_sql: list[str] = []

        def capture_sql(_conn, _cursor, statement, _params, _context, _executemany):
            executed_sql.append(statement)

        fetcher = _RecordingFxFetcher({(d, "BTC", "NZD"): (168500.123456789, "fake")})
        event.listen(self.engine, "before_cursor_execute", capture_sql)
        try:
            repaired = repair_crypto_transfers_and_rates(self.db, fetcher=fetcher)
        finally:
            event.remove(self.engine, "before_cursor_execute", capture_sql)

        self.assertEqual(repaired, 1)
        self.db.refresh(btc_tx)
        self.assertAlmostEqual(float(btc_tx.exchange_rate), 168500.12345679, places=8)

        tx_selects = [sql for sql in executed_sql if "FROM transactions" in sql]
        self.assertTrue(tx_selects)
        first_tx_select = tx_selects[0].upper()
        self.assertIn("WHERE", first_tx_select)
        self.assertIn("CURRENCY", first_tx_select)

    def test_run_startup_fx_repairs_handles_general_and_crypto_legs_with_injected_fetcher(self) -> None:
        d = date(2026, 11, 12)
        nzd_corrupt = Transaction(
            transaction_date=d,
            account_id=self.acc_nzd.id,
            amount=-100.0,
            currency="NZD",
            exchange_rate=35.0,
            merchant="Remit",
            category="General",
            raw_hash="startup-nzd-corrupt",
        )
        sgd_missing = Transaction(
            transaction_date=d,
            account_id=self.acc_sgd.id,
            amount=-50.0,
            currency="SGD",
            exchange_rate=None,
            merchant="Hawker",
            category="Food",
            raw_hash="startup-sgd-missing",
        )
        bad_sgd = Transaction(
            transaction_date=d,
            account_id=self.acc_sgd.id,
            amount=-800.0,
            currency="SGD",
            exchange_rate=1.0,
            merchant="Alex Tan Transfer",
            category="Transfers",
            raw_hash="startup-bad-sgd",
        )
        bad_btc = Transaction(
            transaction_date=d,
            account_id=self.acc_btc.id,
            amount=0.02,
            currency="BTC",
            exchange_rate=0.000006,
            merchant="Bitcoin Deposit",
            category="Transfers",
            raw_hash="startup-bad-btc",
        )
        self.db.add_all([nzd_corrupt, sgd_missing, bad_sgd, bad_btc])
        self.db.flush()
        link_transfer_pair(bad_sgd, bad_btc)
        self.db.commit()

        fetcher = _RecordingFxFetcher(
            {
                (d, "SGD", "NZD"): (1.29, "fake"),
                (d, "BTC", "NZD"): (170000.0, "fake"),
            }
        )
        repaired_fx, repaired_crypto = run_startup_fx_repairs(self.db, fetcher=fetcher)
        self.assertEqual(repaired_fx, 2)
        self.assertGreaterEqual(repaired_crypto, 2)

        for row in (nzd_corrupt, sgd_missing, bad_sgd, bad_btc):
            self.db.refresh(row)

        self.assertEqual(float(nzd_corrupt.exchange_rate), 1.0)
        self.assertAlmostEqual(float(sgd_missing.exchange_rate), 1.29, places=4)
        self.assertIsNone(bad_sgd.linked_transaction_id)
        self.assertIsNone(bad_btc.linked_transaction_id)
        self.assertAlmostEqual(float(bad_sgd.exchange_rate), 1.29, places=4)
        self.assertAlmostEqual(float(bad_btc.exchange_rate), 170000.0, places=2)

    def test_ingestion_service_threads_injected_fx_fetcher_for_txs_and_snapshots(self) -> None:
        d = date(2026, 11, 14)
        fetcher = _RecordingFxFetcher(
            {
                (d, "SGD", "NZD"): (1.3025, "fake"),
                (d + timedelta(days=1), "BTC", "NZD"): (172500.0, "fake"),
            }
        )
        created, skipped, txs = ingest_normalized_transactions(
            self.db,
            self.acc_sgd,
            [
                NormalizedTx(
                    transaction_date=d,
                    amount=-40.0,
                    currency="SGD",
                    merchant="Alex Cafe",
                    category="Food",
                    notes="wise:tx-1001",
                )
            ],
            skip_llm=True,
            fx_fetcher=fetcher,
        )
        self.assertEqual((created, skipped), (1, 0))
        self.assertAlmostEqual(float(txs[0].exchange_rate), 1.3025, places=4)

        snap = upsert_account_snapshot(
            self.db,
            account=self.acc_btc,
            snapshot_date=d + timedelta(days=1),
            balance=0.5,
            currency="BTC",
            source="btc_sync",
            fx_fetcher=fetcher,
        )
        self.assertAlmostEqual(float(snap.exchange_rate), 172500.0, places=2)
        self.assertAlmostEqual(float(snap.balance_nzd), 86250.0, places=2)
        self.assertIn((d, "SGD", "NZD"), fetcher.calls)
        self.assertIn((d + timedelta(days=1), "BTC", "NZD"), fetcher.calls)


if __name__ == "__main__":
    unittest.main()
