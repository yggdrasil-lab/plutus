"""Unit tests for the periodic background sync scheduler."""

from pathlib import Path
import sys
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.schemas import SyncResult
from app.services.sync_scheduler import get_configured_providers, run_scheduled_sync_once

engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class TestSyncScheduler(unittest.TestCase):
    def setUp(self):
        self._orig_akahu_app = settings.akahu_app_token
        self._orig_akahu_user = settings.akahu_user_token
        self._orig_wise = settings.wise_api_token

    def tearDown(self):
        settings.akahu_app_token = self._orig_akahu_app
        settings.akahu_user_token = self._orig_akahu_user
        settings.wise_api_token = self._orig_wise

    def test_get_configured_providers_skips_unconfigured(self):
        settings.akahu_app_token = ""
        settings.akahu_user_token = ""
        settings.wise_api_token = "wise_tok"
        self.assertEqual(get_configured_providers(), ["wise"])

        settings.akahu_app_token = "app"
        settings.akahu_user_token = "user"
        self.assertEqual(get_configured_providers(), ["akahu", "wise"])

    def test_run_scheduled_sync_once_isolates_provider_errors(self):
        def ok_handler(_db, _days):
            return SyncResult(provider="wise", accounts_synced=2, transactions_created=3)

        def failing_handler(_db, _days):
            raise RuntimeError("upstream timeout")

        outcomes = run_scheduled_sync_once(
            db_factory=TestingSessionLocal,
            providers=["akahu", "wise"],
            handlers={"akahu": failing_handler, "wise": ok_handler},
        )
        self.assertTrue(str(outcomes["akahu"]).startswith("error: upstream timeout"))
        self.assertIsInstance(outcomes["wise"], SyncResult)
        self.assertEqual(outcomes["wise"].transactions_created, 3)
