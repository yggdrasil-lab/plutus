from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import Account, AssetSnapshot, Transaction


def test_amortized_recurrence_for_weekly_budget_and_runway():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    try:
        today = date.today()
        with TestingSessionLocal() as db:
            acc = Account(
                name="ANZ Freedom",
                institution="ANZ",
                account_type="checking",
                tier="cash",
                currency="NZD",
            )
            db.add(acc)
            db.flush()
            db.add(
                AssetSnapshot(
                    account_id=acc.id,
                    snapshot_date=today,
                    balance=12200.0,
                    currency="NZD",
                    exchange_rate=1.0,
                    balance_nzd=12200.0,
                    source="manual",
                )
            )
            db.add_all(
                [
                    Transaction(
                        transaction_date=today - timedelta(days=2),
                        account_id=acc.id,
                        amount=-400.0,
                        currency="NZD",
                        merchant="Pak n Save",
                        category="Groceries",
                        raw_hash="rec-var-1",
                        recurrence=None,
                    ),
                    Transaction(
                        transaction_date=today - timedelta(days=10),
                        account_id=acc.id,
                        amount=-520.0,
                        currency="NZD",
                        merchant="Aioi Insurance",
                        category="Insurance",
                        raw_hash="rec-ann-1",
                        recurrence="annual",
                    ),
                    Transaction(
                        transaction_date=today - timedelta(days=5),
                        account_id=acc.id,
                        amount=-52.0,
                        currency="NZD",
                        merchant="Google One",
                        category="Utilities",
                        raw_hash="rec-mon-1",
                        recurrence="monthly",
                    ),
                    Transaction(
                        transaction_date=today - timedelta(days=3),
                        account_id=acc.id,
                        amount=-8000.0,
                        currency="NZD",
                        merchant="Toyota Deposit",
                        category="General",
                        raw_hash="rec-oneoff-1",
                        recurrence="one-off",
                    ),
                ]
            )
            db.commit()

        runway = client.get("/runway").json()
        # 400/4 (100) + 520/52 (10) + 52*12/52 (12) = 122.0; 8000 one-off excluded
        assert runway["breakdown"]["weekly_burn_rate"] == 122.0
        assert runway["breakdown"]["emergency_weeks"] == 100.0

        weekly = client.get("/budgets/breakdown?period=week").json()
        by_cat = {c["category"]: c["total_spend"] for c in weekly["categories"]}
        assert by_cat["Insurance"] == 10.0
        assert by_cat["Utilities"] == 12.0
        assert "General" not in by_cat

        # Test PATCH /transactions/{id} recurrence update
        patch_res = client.patch("/transactions/1", json={"recurrence": "weekly"})
        assert patch_res.status_code == 200
        assert patch_res.json()["recurrence"] == "weekly"

        # Test POST /rules with target_recurrence
        rule_res = client.post(
            "/rules",
            json={
                "name": "Google One Monthly",
                "description_pattern": "Google One",
                "match_type": "contains",
                "target_category": "Utilities",
                "target_recurrence": "monthly",
                "apply_retroactively": True,
            },
        )
        assert rule_res.status_code == 201
        assert rule_res.json()["target_recurrence"] == "monthly"
    finally:
        app.dependency_overrides.clear()
