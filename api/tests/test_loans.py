from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import Account, AssetSnapshot


def test_loans_countdown_net_worth_and_time_machine():
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
                    balance=100000.0,
                    currency="NZD",
                    exchange_rate=1.0,
                    balance_nzd=100000.0,
                    source="manual",
                )
            )
            db.commit()

        res = client.post(
            "/loans",
            json={
                "name": "Toyota Corolla Cross (TFS)",
                "lender": "Toyota Financial Services",
                "principal_amount": 45399.42,
                "annual_interest_rate": 0.0962,
                "term_months": 36,
                "monthly_payment": 1452.42,
                "start_date": "2026-11-20",
                "asset_value": 55000.0,
            },
        )
        assert res.status_code == 201
        loan = res.json()
        assert loan["payments_remaining"] == 36
        assert loan["remaining_principal"] == 45399.42
        assert loan["payoff_date"] == "2029-10-20"
        assert loan["weekly_payment_equivalent"] == 335.17

        summary = client.get("/loans").json()
        assert summary["total_liabilities"] == 45399.42
        assert summary["total_monthly_payment"] == 1452.42

        runway = client.get("/runway").json()
        assert runway["breakdown"]["total_liabilities"] == 45399.42
        assert runway["breakdown"]["net_worth"] == round(100000.0 - 45399.42, 2)

        sim = client.post("/runway/simulate", json={"pay_off_loans": True}).json()
        assert sim["total_liabilities"] == 0.0
        assert sim["loan_weekly_relief"] == 335.17
        assert sim["loan_free_date"] == "2029-10-20"
    finally:
        app.dependency_overrides.clear()
