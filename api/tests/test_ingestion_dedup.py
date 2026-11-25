from datetime import date
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Account, Transaction
from app.services.ai_gateway import InMemoryAiGateway, JevGateRequest, get_ai_gateway
from app.services.ingestion_service import NormalizedTx, ingest_normalized_transactions


def test_same_day_identical_amount_merchant_with_distinct_external_ids():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as db:
        acc = Account(
            name="ANZ Low Rate Visa",
            institution="ANZ",
            account_type="credit_card",
            tier="cash",
            currency="NZD",
        )
        db.add(acc)
        db.commit()
        db.refresh(acc)

        batch = [
            NormalizedTx(
                transaction_date=date(2026, 11, 10),
                amount=-20.0,
                currency="NZD",
                merchant="Mojo Coffee",
                raw_description="MOJO COFFEE",
                category="Food",
                notes="akahu:trans_aaa111",
            ),
            NormalizedTx(
                transaction_date=date(2026, 11, 10),
                amount=-20.0,
                currency="NZD",
                merchant="Mojo Coffee",
                raw_description="MOJO COFFEE",
                category="Food",
                notes="akahu:trans_bbb222",
            ),
        ]

        created, skipped, _ = ingest_normalized_transactions(db, acc, batch)
        assert created == 2
        assert skipped == 0

        # Re-syncing the exact same batch must skip both without duplicates or overwrites.
        created2, skipped2, _ = ingest_normalized_transactions(db, acc, batch)
        assert created2 == 0
        assert skipped2 == 2
        assert db.query(Transaction).count() == 2


def test_jev_tags_applied_when_untagged_even_with_category():
    import json

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as db:
        acc = Account(
            name="ANZ Freedom",
            institution="ANZ",
            account_type="checking",
            tier="cash",
            currency="NZD",
        )
        db.add(acc)
        db.commit()
        db.refresh(acc)

        gateway = InMemoryAiGateway(
            jev_responses=[
                json.dumps({"category": "Insurance", "is_anomaly": False, "tags": ["car", "insurance"]})
            ]
        )
        created, _, txs = ingest_normalized_transactions(
            db,
            acc,
            [
                NormalizedTx(
                    transaction_date=date(2026, 11, 1),
                    amount=-1786.72,
                    currency="NZD",
                    merchant="Aioi Nissay Dowa Insurance",
                    category="Insurance",
                    notes="akahu:trans_ins_1",
                )
            ],
            gateway=gateway,
        )
        assert created == 1
        assert len(gateway.jev_requests) == 1
        assert txs[0].category == "Insurance"
        assert json.loads(txs[0].tags) == ["car"]


def test_skip_llm_bypasses_jev_and_uses_heuristics():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as db:
        acc = Account(
            name="ANZ Freedom",
            institution="ANZ",
            account_type="checking",
            tier="cash",
            currency="NZD",
        )
        db.add(acc)
        db.commit()
        db.refresh(acc)

        gateway = InMemoryAiGateway()
        created, _, txs = ingest_normalized_transactions(
            db,
            acc,
            [
                NormalizedTx(
                    transaction_date=date(2026, 11, 2),
                    amount=-85.00,
                    currency="NZD",
                    merchant="PAK N SAVE ALBANY",
                    category="General",
                    notes="akahu:trans_skip_1",
                )
            ],
            skip_llm=True,
            gateway=gateway,
        )
        assert gateway.jev_requests == []
        assert created == 1
        assert txs[0].category == "Groceries"


def test_wise_parse_iso_date_uses_configured_timezone():
    from app.services.connectors import parse_iso_date

    # 2026-10-27 12:00:00Z is 2026-10-28 01:00:00 NZDT (UTC+13)
    assert parse_iso_date("2026-10-27T12:00:00.000Z") == date(2026, 10, 28)
    assert parse_iso_date("2026-10-27T01:00:00.000Z") == date(2026, 10, 27)


def test_ingestion_shares_tag_vocab_and_custom_categories_with_jev():
    import json
    from app.models import Category

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as db:
        acc = Account(
            name="ANZ Freedom",
            institution="ANZ",
            account_type="checking",
            tier="cash",
            currency="NZD",
        )
        db.add(acc)
        db.add(Category(name="Pets", is_active=True, is_system=False, sort_order=150))
        db.commit()
        db.refresh(acc)

        db.add(
            Transaction(
                transaction_date=date(2026, 11, 1),
                account_id=acc.id,
                amount=-20.0,
                currency="NZD",
                merchant="Existing Merchant",
                category="Dining",
                raw_hash="seed-vocab-tx",
                tags=json.dumps(["coffee", "general"]),
            )
        )
        db.commit()

        def jev_handler(req: JevGateRequest) -> str:
            if "Animates" in req.user_content:
                return json.dumps(
                    {"category": "Pets", "is_anomaly": False, "tags": ["pets", "general", "pet-food"]}
                )
            return json.dumps(
                {"category": "Insurance", "is_anomaly": False, "tags": ["insurance", "general", "car"]}
            )

        gateway = InMemoryAiGateway(jev_handler=jev_handler)
        created, _, txs = ingest_normalized_transactions(
            db,
            acc,
            [
                NormalizedTx(
                    transaction_date=date(2026, 11, 10),
                    amount=-55.0,
                    currency="NZD",
                    merchant="Animates",
                    category="General",
                    notes="akahu:trans_pets_1",
                ),
                NormalizedTx(
                    transaction_date=date(2026, 11, 11),
                    amount=-120.0,
                    currency="NZD",
                    merchant="AA Insurance",
                    category="Insurance",
                    notes="akahu:trans_ins_2",
                ),
            ],
            gateway=gateway,
        )

        assert created == 2
        assert len(gateway.jev_requests) == 2
        for req in gateway.jev_requests:
            assert "Existing tags: coffee" in req.user_content
            assert "Additional custom categories: Pets" in req.user_content
            assert '"coffee"' in req.system_prompt
            assert '"Pets"' in req.system_prompt
        assert txs[0].category == "Pets"
        assert json.loads(txs[0].tags) == ["pet-food"]
        assert txs[1].category == "Insurance"
        assert json.loads(txs[1].tags) == ["car"]


def test_quick_log_shares_vocab_custom_categories_and_strips_redundant_tags():
    import json
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import StaticPool
    from app.db import get_db
    from app.main import app
    from app.models import Category

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        with SessionLocal() as db:
            acc = Account(
                name="ANZ Checking",
                institution="ANZ",
                account_type="checking",
                tier="cash",
                currency="NZD",
            )
            db.add(acc)
            db.add(Category(name="Pets", is_active=True, is_system=False, sort_order=150))
            db.commit()
            db.refresh(acc)
            db.add(
                Transaction(
                    transaction_date=date(2026, 11, 1),
                    account_id=acc.id,
                    amount=-12.0,
                    currency="NZD",
                    merchant="Local Cafe",
                    category="Dining",
                    raw_hash="ql-seed-1",
                    tags=json.dumps(["oat-latte", "general"]),
                )
            )
            db.commit()

        gateway = InMemoryAiGateway(
            jev_responses=[
                json.dumps(
                    {
                        "category": "General",
                        "is_anomaly": False,
                        "tags": ["General", "#Groceries", "weekly shop"],
                    }
                )
            ]
        )
        app.dependency_overrides[get_ai_gateway] = lambda: gateway

        client = TestClient(app)
        res = client.post(
            "/integrations/quick-log",
            json={
                "amount": 48.50,
                "merchant": "Pak n Save",
                "category": "General",
                "transaction_date": "2026-11-15",
            },
        )
        assert res.status_code == 201
        data = res.json()
        assert data["category"] == "Groceries"
        assert data["tags"] == ["weekly-shop"]
        assert len(gateway.jev_requests) == 1
        sent = gateway.jev_requests[0]
        assert "Existing tags: oat-latte" in sent.user_content
        assert "Additional custom categories: Pets" in sent.user_content
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_ai_gateway, None)
        Base.metadata.drop_all(bind=engine)
