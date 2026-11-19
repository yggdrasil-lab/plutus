from datetime import date
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Account, Transaction
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
    from unittest.mock import patch
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

        with patch(
            "app.services.ingestion_service.classify_transaction_jev_with_tags",
            return_value=("Insurance", False, ["car", "insurance"]),
        ):
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
            )
        assert created == 1
        assert txs[0].category == "Insurance"
        assert json.loads(txs[0].tags) == ["car"]


def test_skip_llm_bypasses_jev_and_uses_heuristics():
    from unittest.mock import patch

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

        with patch("app.services.ingestion_service.classify_transaction_jev_with_tags") as mock_jev:
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
            )
        mock_jev.assert_not_called()
        assert created == 1
        assert txs[0].category == "Groceries"


def test_wise_parse_iso_date_uses_configured_timezone():
    from app.services.connectors.wise import _parse_iso_date

    # 2026-10-27 12:00:00Z is 2026-10-28 01:00:00 NZDT (UTC+13)
    assert _parse_iso_date("2026-10-27T12:00:00.000Z") == date(2026, 10, 28)
    assert _parse_iso_date("2026-10-27T01:00:00.000Z") == date(2026, 10, 27)


def test_ingestion_shares_tag_vocab_and_custom_categories_with_jev():
    import json
    from unittest.mock import patch
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

        seen_calls = []

        def fake_jev(merchant, amount, notes=None, existing_tags=None, extra_categories=None):
            seen_calls.append(
                {
                    "merchant": merchant,
                    "existing_tags": existing_tags,
                    "extra_categories": extra_categories,
                }
            )
            if "Animates" in merchant:
                return ("Pets", False, ["pets", "general", "pet-food"])
            return ("Insurance", False, ["insurance", "general", "car"])

        with patch("app.services.ingestion_service.classify_transaction_jev_with_tags", side_effect=fake_jev):
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
            )

        assert created == 2
        assert len(seen_calls) == 2
        for call in seen_calls:
            assert call["existing_tags"] == ["coffee"]
            assert call["extra_categories"] == ["Pets"]
        assert txs[0].category == "Pets"
        assert json.loads(txs[0].tags) == ["pet-food"]
        assert txs[1].category == "Insurance"
        assert json.loads(txs[1].tags) == ["car"]


def test_quick_log_shares_vocab_custom_categories_and_strips_redundant_tags():
    import json
    from unittest.mock import patch
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

        captured = {}

        def fake_jev(merchant, amount, notes=None, existing_tags=None, extra_categories=None):
            captured["existing_tags"] = existing_tags
            captured["extra_categories"] = extra_categories
            # Simulate Jev returning General + tags where 'groceries' matches auto_categorize fallback
            return ("General", False, ["General", "#Groceries", "weekly shop"])

        client = TestClient(app)
        with patch("app.routers.integrations.classify_transaction_jev_with_tags", side_effect=fake_jev):
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
        assert captured["existing_tags"] == ["oat-latte"]
        assert captured["extra_categories"] == ["Pets"]
    finally:
        app.dependency_overrides.pop(get_db, None)
        Base.metadata.drop_all(bind=engine)
