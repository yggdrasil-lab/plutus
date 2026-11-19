"""Unit tests for Akahu category normalization and Transaction tags."""

from datetime import date
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
from app.models import Account
from app.services.connectors.akahu import normalize_akahu_category, normalize_akahu_feeds

TEST_DB_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class TestTransactionTagsAndAkahuCategories(unittest.TestCase):
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

        self.acc = Account(
            name="ANZ Freedom",
            account_type="checking",
            tier="cash",
            institution="ANZ",
            currency="NZD",
        )
        self.db.add(self.acc)
        self.db.commit()
        self.db.refresh(self.acc)

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def test_akahu_category_normalizes_to_canonical_and_preserves_tag(self):
        cat, tags = normalize_akahu_category(
            "Gyms, fitness, aquatic facilities, yoga, pilates", "Les Mills", "LES MILLS NZ"
        )
        self.assertEqual(cat, "Fitness")
        self.assertEqual(tags, ("gym", "subscription"))

        feeds = normalize_akahu_feeds(
            [
                {
                    "_id": "acc_1",
                    "name": "ANZ Freedom",
                    "type": "CHECKING",
                    "connection": {"name": "ANZ"},
                    "balance": {"current": 1200.0, "currency": "NZD"},
                }
            ],
            [
                {
                    "_id": "tx_1",
                    "_account": "acc_1",
                    "date": "2026-11-15T10:00:00Z",
                    "amount": -42.0,
                    "description": "LES MILLS MEMBERSHIP",
                    "merchant": {"name": "Les Mills"},
                    "category": {"name": "Gyms and fitness"},
                }
            ],
        )
        self.assertEqual(feeds[0].transactions[0].category, "Fitness")
        self.assertEqual(feeds[0].transactions[0].tags, ("gym", "subscription"))

    def test_akahu_utc_timestamp_converts_to_auckland_local_date(self):
        from datetime import date
        from app.services.connectors.akahu import _parse_iso_date

        # 2026-11-01T12:30:00Z is 2026-11-02 01:30 NZDT (UTC+13)
        self.assertEqual(_parse_iso_date("2026-11-01T12:30:00Z"), date(2026, 11, 2))

    def test_create_update_and_filter_transaction_tags(self):
        res_create = self.client.post(
            "/transactions",
            json={
                "transaction_date": date(2026, 11, 15).isoformat(),
                "account_id": self.acc.id,
                "amount": -24.0,
                "currency": "NZD",
                "merchant": "North Harbour Badminton",
                "category": "Health",
                "tags": ["#Badminton", "Social", "badminton"],
            },
        )
        self.assertEqual(res_create.status_code, 201)
        tx = res_create.json()
        self.assertEqual(tx["tags"], ["badminton", "social"])

        res_patch = self.client.patch(
            f"/transactions/{tx['id']}",
            json={"tags": ["badminton", "social", "sports"]},
        )
        self.assertEqual(res_patch.status_code, 200)
        self.assertEqual(res_patch.json()["tags"], ["badminton", "social", "sports"])

        res_filter = self.client.get("/transactions?tag=badminton")
        self.assertEqual(res_filter.status_code, 200)
        self.assertEqual(len(res_filter.json()), 1)
        self.assertEqual(res_filter.json()[0]["id"], tx["id"])

        res_miss = self.client.get("/transactions?tag=coffee")
        self.assertEqual(res_miss.status_code, 200)
        self.assertEqual(len(res_miss.json()), 0)


if __name__ == "__main__":
    unittest.main()
