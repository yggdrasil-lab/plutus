"""Unit tests for the watch-only Bitcoin connector."""

from datetime import date
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base
from app.models import Account, AssetSnapshot, ExchangeRate
from app.services.connectors import ConnectorConfigError
from app.services.connectors.bitcoin import (
    HttpBitcoinGateway,
    derive_address_from_xpub,
    normalize_bitcoin_feed,
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

        gateway = HttpBitcoinGateway(f"BitBox02:{BIP84_TEST_ZPUB}", gap_limit=2)
        with patch("httpx.Client.get", side_effect=lambda url, **_kw: fake_get(url)):
            feeds = gateway.fetch_feeds()

        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0].account.currency, "BTC")
        self.assertEqual(feeds[0].account.account_type, "crypto")
        self.assertEqual(feeds[0].account.tier, "growth")
        self.assertAlmostEqual(feeds[0].account.current_balance, 0.12623456, places=8)

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


if __name__ == "__main__":
    unittest.main()
