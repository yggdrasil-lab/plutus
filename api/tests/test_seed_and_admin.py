"""Unit tests for Plutus data seeding and administrative reset."""

import sys
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Add api directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base
from app.models import Account, AssetSnapshot, BudgetRule, Transaction
from app.services.seed import clear_all_data, seed_initial_data


class TestSeedAndAdmin(unittest.TestCase):
    """Test seeding initial financial baseline and clearing data on fresh database."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)

    def test_seed_initial_data(self):
        """Seed should create 4 accounts, 4 snapshots, 6 budget rules, and historical transactions."""
        result = seed_initial_data(self.db)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["accounts_created"], 4)
        self.assertEqual(result["cash_balance"], 24000.0)
        self.assertEqual(result["growth_balance"], 160000.0)
        self.assertEqual(result["locked_balance"], 85000.0)
        self.assertEqual(result["total_liquid"], 184000.0)
        self.assertEqual(result["weekly_budget"], 1000.0)

        # Verify DB counts
        accounts = self.db.query(Account).all()
        self.assertEqual(len(accounts), 4)

        snapshots = self.db.query(AssetSnapshot).all()
        self.assertEqual(len(snapshots), 4)

        rules = self.db.query(BudgetRule).all()
        self.assertEqual(len(rules), 6)

        txs = self.db.query(Transaction).all()
        self.assertGreater(len(txs), 20)

    def test_clear_all_data_preserve_accounts(self):
        """Clearing data without clear_accounts should remove transactions and snapshots but keep accounts."""
        seed_initial_data(self.db)

        clear_res = clear_all_data(self.db, clear_accounts=False)
        self.assertEqual(clear_res["status"], "success")

        # Accounts preserved
        accounts = self.db.query(Account).all()
        self.assertEqual(len(accounts), 4)

        # Snapshots, transactions, rules cleared
        self.assertEqual(len(self.db.query(AssetSnapshot).all()), 0)
        self.assertEqual(len(self.db.query(Transaction).all()), 0)
        self.assertEqual(len(self.db.query(BudgetRule).all()), 0)

    def test_clear_all_data_including_accounts(self):
        """Clearing data with clear_accounts=True should wipe everything."""
        seed_initial_data(self.db)

        clear_res = clear_all_data(self.db, clear_accounts=True)
        self.assertEqual(clear_res["status"], "success")
        self.assertEqual(clear_res["cleared_accounts"], 4)

        self.assertEqual(len(self.db.query(Account).all()), 0)
        self.assertEqual(len(self.db.query(AssetSnapshot).all()), 0)
        self.assertEqual(len(self.db.query(Transaction).all()), 0)


if __name__ == "__main__":
    unittest.main()
