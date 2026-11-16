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
        assert json.loads(txs[0].tags) == ["car", "insurance"]
