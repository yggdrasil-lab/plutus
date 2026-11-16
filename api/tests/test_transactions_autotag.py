from datetime import date
import json
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import Account, Transaction


def test_auto_tag_transactions_with_jev():
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
        with TestingSessionLocal() as db:
            acc = Account(
                name="ANZ",
                institution="ANZ",
                account_type="checking",
                tier="cash",
                currency="NZD",
            )
            db.add(acc)
            db.flush()
            db.add_all(
                [
                    Transaction(
                        transaction_date=date(2026, 11, 15),
                        account_id=acc.id,
                        amount=-18.5,
                        currency="NZD",
                        merchant="Mojo Coffee",
                        category="Food",
                        raw_hash="auto-tag-1",
                        tags="[]",
                    ),
                    Transaction(
                        transaction_date=date(2026, 11, 14),
                        account_id=acc.id,
                        amount=-28.81,
                        currency="NZD",
                        merchant="CityFitness",
                        category="Health",
                        raw_hash="auto-tag-2",
                        tags='["gym-membership"]',
                    ),
                ]
            )
            db.commit()

        with patch(
            "app.services.jev_client.classify_transaction_jev_with_tags",
            return_value=("Food", False, ["coffee", "cafe"]),
        ):
            res = client.post("/transactions/auto-tag")

        assert res.status_code == 200
        body = res.json()
        assert body["evaluated"] == 1
        assert body["updated"] == 1
        assert body["samples"][0]["merchant"] == "Mojo Coffee"
        assert body["samples"][0]["tags"] == ["coffee", "cafe"]

        with TestingSessionLocal() as db:
            txs = db.query(Transaction).order_by(Transaction.id).all()
            assert json.loads(txs[0].tags) == ["coffee", "cafe"]
            assert json.loads(txs[1].tags) == ["gym-membership"]
    finally:
        app.dependency_overrides.clear()
