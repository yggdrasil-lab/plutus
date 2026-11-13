"""Unit tests for Plutus institutions directory and dynamic suggestions."""

import sys
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Add api directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base
from app.models import Account
from app.routers.institutions import list_institutions


class TestInstitutionsRouter(unittest.TestCase):
    """Test retrieving known institutions and dynamic account institution merging."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(self.engine)

    def test_list_institutions_defaults(self):
        """Default institutions should include standard New Zealand and global options."""
        insts = list_institutions(self.db)
        self.assertGreaterEqual(len(insts), 10)
        names = {i.name for i in insts}
        self.assertIn("ASB", names)
        self.assertIn("ANZ", names)
        self.assertIn("Sharesies", names)
        self.assertIn("Simplicity", names)
        self.assertIn("Milford", names)
        self.assertIn("DBS Bank", names)

        dbs_info = next(i for i in insts if i.name == "DBS Bank")
        self.assertEqual(dbs_info.currency, "SGD")

    def test_list_institutions_merges_custom_account(self):
        """Custom institutions from active accounts should automatically appear in directory."""
        custom_acc = Account(
            name="Crypto Reserve",
            account_type="crypto",
            tier="growth",
            institution="Kraken",
            currency="NZD",
            expected_annual_return=0.10,
            is_active=True,
        )
        self.db.add(custom_acc)
        self.db.commit()

        insts = list_institutions(self.db)
        names = {i.name for i in insts}
        self.assertIn("Kraken", names)

        kraken_info = next(i for i in insts if i.name == "Kraken")
        self.assertEqual(kraken_info.default_tier, "growth")
        self.assertEqual(kraken_info.default_type, "crypto")


if __name__ == "__main__":
    unittest.main()
