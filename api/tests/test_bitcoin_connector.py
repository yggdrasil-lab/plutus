"""Unit tests for the watch-only Bitcoin connector."""

from datetime import date
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base
from app.models import Account, AssetSnapshot, ExchangeRate, Transaction
from app.services.connectors import ConnectorConfigError
from app.services.connectors.bitcoin import (
    HttpBitcoinGateway,
    derive_address_from_xpub,
    normalize_bitcoin_feed,
    normalize_bitcoin_transactions,
    parse_watch_sources,
    sum_address_satoshis,
    sync_bitcoin,
)

# Official BIP84 test vector account-level zpub (m/84'/0'/0')
BIP84_TEST_ZPUB = (
    "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs"
)

TEST_DB_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class TestBitcoinConnector(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_derive_address_from_bip84_zpub_vector(self):
        self.assertEqual(
            derive_address_from_xpub(BIP84_TEST_ZPUB, 0, 0),
            "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu",
        )
        self.assertEqual(
            derive_address_from_xpub(BIP84_TEST_ZPUB, 0, 1),
            "bc1qnjg0jd8228aq7egyzacy8cys3knf9xvrerkf9g",
        )

    def test_parse_watch_sources_validation(self):
        parsed = parse_watch_sources(
            f"BitBox02:{BIP84_TEST_ZPUB}, Cold:bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
        )
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0].label, "BitBox02")
        self.assertEqual(parsed[0].kind, "xpub")
        self.assertEqual(parsed[1].label, "Cold")
        self.assertEqual(parsed[1].kind, "address")

        with self.assertRaises(ConnectorConfigError):
            parse_watch_sources("")

        with self.assertRaises(ConnectorConfigError):
            parse_watch_sources("Bad:not-a-key")

    def test_sum_address_satoshis_and_gap_limit_scan(self):
        addr0 = derive_address_from_xpub(BIP84_TEST_ZPUB, 0, 0)
        addr1 = derive_address_from_xpub(BIP84_TEST_ZPUB, 0, 1)

        def fake_get(url):
            resp = MagicMock()
            resp.status_code = 200
            if url.endswith(addr0):
                resp.json.return_value = {
                    "chain_stats": {"funded_txo_sum": 15_000_000, "spent_txo_sum": 5_000_000, "tx_count": 2},
                    "mempool_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0, "tx_count": 0},
                }
            elif url.endswith(addr1):
                resp.json.return_value = {
                    "chain_stats": {"funded_txo_sum": 2_500_000, "spent_txo_sum": 0, "tx_count": 1},
                    "mempool_stats": {"funded_txo_sum": 123_456, "spent_txo_sum": 0, "tx_count": 1},
                }
            else:
                resp.json.return_value = {
                    "chain_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0, "tx_count": 0},
                    "mempool_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0, "tx_count": 0},
                }
            return resp

        sats, has_hist = sum_address_satoshis(
            {
                "chain_stats": {"funded_txo_sum": 1000, "spent_txo_sum": 250, "tx_count": 1},
                "mempool_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0, "tx_count": 0},
            }
        )
        self.assertEqual(sats, 750)
        self.assertTrue(has_hist)

        gateway = HttpBitcoinGateway(f"BitBox02:{BIP84_TEST_ZPUB}", gap_limit=2, request_delay=0.0)
        with patch("httpx.Client.get", side_effect=lambda url, **_kw: fake_get(url)):
            feeds = gateway.fetch_feeds()

        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0].account.currency, "BTC")
        self.assertEqual(feeds[0].account.account_type, "crypto")
        self.assertEqual(feeds[0].account.tier, "growth")
        self.assertAlmostEqual(feeds[0].account.current_balance, 0.12623456, places=8)

    def test_get_address_stats_retries_on_429(self):
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "1"}
        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.json.return_value = {
            "chain_stats": {"funded_txo_sum": 50_000, "spent_txo_sum": 0, "tx_count": 1},
            "mempool_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0, "tx_count": 0},
        }
        resp_txs = MagicMock()
        resp_txs.status_code = 200
        resp_txs.json.return_value = []
        gateway = HttpBitcoinGateway(
            "Cold:bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu",
            request_delay=0.0,
        )
        with (
            patch("httpx.Client.get", side_effect=[resp_429, resp_ok, resp_txs]),
            patch("app.services.connectors.bitcoin.time.sleep") as mock_sleep,
        ):
            feeds = gateway.fetch_feeds()
        mock_sleep.assert_called_once_with(1.0)
        self.assertAlmostEqual(feeds[0].account.current_balance, 0.0005, places=8)

    def test_sync_bitcoin_creates_account_and_nzd_snapshot(self):
        self.db.add(
            ExchangeRate(
                rate_date=date.today(),
                from_currency="BTC",
                to_currency="NZD",
                rate=150000.0,
                source="coingecko",
            )
        )
        self.db.commit()

        source = parse_watch_sources(f"BitBox02:{BIP84_TEST_ZPUB}")[0]
        feed = normalize_bitcoin_feed(source, 25_000_000, addresses_scanned=5)
        fake_gateway = MagicMock()
        fake_gateway.fetch_feeds.return_value = [feed]

        res = sync_bitcoin(self.db, gateway=fake_gateway)
        self.assertEqual(res.provider, "bitcoin")
        self.assertEqual(res.accounts_synced, 1)
        self.assertEqual(res.snapshots_updated, 1)

        acc = self.db.query(Account).filter(Account.external_provider == "bitcoin").one()
        self.assertEqual(acc.name, "BitBox02")
        self.assertEqual(acc.account_type, "crypto")
        self.assertEqual(acc.tier, "growth")
        self.assertEqual(acc.currency, "BTC")

        snap = self.db.query(AssetSnapshot).filter(AssetSnapshot.account_id == acc.id).one()
        self.assertAlmostEqual(float(snap.balance), 0.25, places=8)
        self.assertAlmostEqual(float(snap.balance_nzd), 37500.0, places=2)

    def test_bitcoin_onchain_transactions_normalized_and_synced_idempotently(self):
        self.db.add(
            ExchangeRate(
                rate_date=date.today(),
                from_currency="BTC",
                to_currency="NZD",
                rate=150000.0,
                source="coingecko",
            )
        )
        self.db.commit()

        addr0 = derive_address_from_xpub(BIP84_TEST_ZPUB, 0, 0)
        change0 = derive_address_from_xpub(BIP84_TEST_ZPUB, 1, 0)

        deposit_tx = {
            "txid": "a" * 64,
            "fee": 400,
            "status": {"confirmed": True, "block_time": 1731240000},  # 2024-11-10 UTC
            "vin": [{"prevout": {"scriptpubkey_address": "bc1qexternal", "value": 2_000_400}}],
            "vout": [{"scriptpubkey_address": addr0, "value": 2_000_000}],
        }
        spend_with_change_tx = {
            "txid": "b" * 64,
            "fee": 500,
            "status": {"confirmed": True, "block_time": 1742040000},  # 2025-03-15 UTC
            "vin": [{"prevout": {"scriptpubkey_address": addr0, "value": 2_000_000}}],
            "vout": [
                {"scriptpubkey_address": "bc1qmerchant", "value": 500_000},
                {"scriptpubkey_address": change0, "value": 1_499_500},
            ],
        }

        norm = normalize_bitcoin_transactions([deposit_tx, spend_with_change_tx, deposit_tx], {addr0, change0})
        self.assertEqual(len(norm), 2)
        self.assertEqual(norm[0].amount, -0.005005)
        self.assertEqual(norm[0].fee_amount, 0.000005)
        self.assertEqual(norm[0].category, "Transfers")
        self.assertEqual(norm[0].notes, f"btc:{'b' * 64}")
        self.assertEqual(norm[1].amount, 0.02)
        self.assertEqual(norm[1].notes, f"btc:{'a' * 64}")

        def fake_get(url):
            resp = MagicMock()
            resp.status_code = 200
            if url.endswith(f"/address/{addr0}"):
                resp.json.return_value = {
                    "chain_stats": {"funded_txo_sum": 2_000_000, "spent_txo_sum": 2_000_000, "tx_count": 2},
                    "mempool_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0, "tx_count": 0},
                }
            elif url.endswith(f"/address/{addr0}/txs"):
                resp.json.return_value = [deposit_tx, spend_with_change_tx]
            elif url.endswith(f"/address/{change0}"):
                resp.json.return_value = {
                    "chain_stats": {"funded_txo_sum": 1_499_500, "spent_txo_sum": 0, "tx_count": 1},
                    "mempool_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0, "tx_count": 0},
                }
            elif url.endswith(f"/address/{change0}/txs"):
                resp.json.return_value = [spend_with_change_tx]
            else:
                resp.json.return_value = {
                    "chain_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0, "tx_count": 0},
                    "mempool_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0, "tx_count": 0},
                }
            return resp

        gateway = HttpBitcoinGateway(f"BitBox02:{BIP84_TEST_ZPUB}", gap_limit=1, request_delay=0.0)
        with (
            patch("httpx.Client.get", side_effect=lambda url, **_kw: fake_get(url)),
            patch("app.services.fx_service.HttpFxRateFetcher._fetch_coingecko", return_value=None) as mock_cg,
        ):
            res1 = sync_bitcoin(self.db, gateway=gateway)
            res2 = sync_bitcoin(self.db, gateway=gateway)

        mock_cg.assert_not_called()
        self.assertEqual(res1.transactions_created, 2)
        self.assertEqual(res1.transactions_skipped, 0)
        self.assertAlmostEqual(res1.accounts[0].balance, 0.014995, places=8)
        self.assertEqual(res2.transactions_created, 0)
        self.assertEqual(res2.transactions_skipped, 2)

        txs = self.db.scalars(select(Transaction).order_by(Transaction.transaction_date.asc())).all()
        self.assertEqual(len(txs), 2)
        self.assertAlmostEqual(float(txs[0].amount), 0.02, places=8)
        self.assertAlmostEqual(float(txs[1].amount), -0.005005, places=8)
        self.assertEqual(txs[0].category, "Transfers")
        self.assertAlmostEqual(float(txs[0].exchange_rate), 150000.0, places=2)


if __name__ == "__main__":
    unittest.main()
