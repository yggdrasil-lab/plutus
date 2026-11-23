"""SQLite :memory: interface tests for the deep Product Catalog package."""

from collections.abc import Iterator
from dataclasses import FrozenInstanceError, is_dataclass
from datetime import date
from pathlib import Path
import sys

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Base
from app.models import Product, Receipt, ReceiptItem
from app.services.product_catalog import (
    PricePointSnapshot,
    ProductConflictError,
    ProductCreateInput,
    ProductListQuery,
    ProductListResult,
    ProductNotFoundError,
    ProductRef,
    ProductSnapshot,
    ProductStatisticSnapshot,
    ProductStatisticsSnapshot,
    ProductUpdateInput,
    ProductValidationError,
    backfill_unlinked_receipt_item_products,
    compute_product_statistics,
    create_product,
    delete_product,
    get_or_create_product,
    get_product_snapshot,
    list_products,
    update_product,
)


def _new_memory_session_factory(
    *, enable_fk: bool = False
) -> tuple[sessionmaker[Session], object]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    if enable_fk:

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _conn_record):  # type: ignore[no-untyped-def]
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine), engine


@pytest.fixture()
def db_session() -> Iterator[Session]:
    """Provide an isolated SQLite :memory: session."""
    session_factory, engine = _new_memory_session_factory()
    db = session_factory()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def _add_receipt_with_item(
    db: Session,
    *,
    receipt_date: date,
    merchant: str,
    item_name: str,
    unit_price: float,
    total_price: float,
    product_id: int | None = None,
    domain: str | None = None,
    product_type: str | None = None,
    discount_amount: float | None = None,
) -> ReceiptItem:
    receipt = Receipt(
        receipt_date=receipt_date,
        merchant=merchant,
        total_amount=total_price,
        currency="NZD",
        status="unlinked",
    )
    db.add(receipt)
    db.flush()
    item = ReceiptItem(
        receipt_id=receipt.id,
        product_id=product_id,
        item_name=item_name,
        quantity=1.0,
        unit_price=unit_price,
        discount_amount=discount_amount,
        total_price=total_price,
        category="Supplements",
        domain=domain,
        product_type=product_type,
    )
    db.add(item)
    db.flush()
    return item


def test_detached_frozen_snapshots_survive_session_close() -> None:
    session_factory, engine = _new_memory_session_factory()
    try:
        with session_factory() as db:
            created = create_product(
                db,
                ProductCreateInput(
                    name="Nordic Oats 1kg",
                    brand="Harbour Mill",
                    category="Groceries",
                    domain="general",
                    tags=("oats", "breakfast"),
                ),
            )
            _add_receipt_with_item(
                db,
                receipt_date=date(2026, 9, 1),
                merchant="Harbour Market",
                item_name="Nordic Oats 1kg",
                unit_price=8.50,
                total_price=8.50,
                product_id=created.id,
                domain="general",
                product_type="grocery",
            )
            db.commit()
            listed = list_products(db)
            detail = get_product_snapshot(db, created.id)
            stats = compute_product_statistics(db)

        assert is_dataclass(detail) and isinstance(detail, ProductSnapshot)
        assert is_dataclass(listed) and isinstance(listed, ProductListResult)
        assert is_dataclass(stats) and isinstance(stats, ProductStatisticsSnapshot)
        assert isinstance(detail.price_history[0], PricePointSnapshot)
        assert isinstance(stats.products[0], ProductStatisticSnapshot)
        assert detail.purchase_count == 1
        assert detail.latest_price == 8.50
        assert listed.all_tags == ("breakfast", "oats")
        assert stats.total_products == 1

        with pytest.raises(FrozenInstanceError):
            detail.name = "Mutated"  # type: ignore[misc]
        with pytest.raises(FrozenInstanceError):
            stats.products[0].total_spent = 999.0  # type: ignore[misc]
    finally:
        Base.metadata.drop_all(bind=engine)


def test_get_or_create_product_returns_detached_frozen_product_ref() -> None:
    session_factory, engine = _new_memory_session_factory()
    try:
        with session_factory() as db:
            ref = get_or_create_product(
                db,
                "Marine Hydration Gel",
                brand="Harbour Skin",
                category="Personal Care",
                domain="02-skin",
                tags=["skincare", "moisturizer"],
                volume_ml="50ml",
            )
            db.commit()

        assert is_dataclass(ref) and isinstance(ref, ProductRef)
        assert isinstance(ref.id, int)
        assert ref.name == "Marine Hydration Gel"
        assert ref.brand == "Harbour Skin"
        assert ref.category == "Personal Care"
        assert ref.domain == "02-skin"
        assert ref.tags == ("skincare", "moisturizer")
        assert ref.volume_ml == "50ml"
        with pytest.raises(FrozenInstanceError):
            ref.name = "Mutated"  # type: ignore[misc]
    finally:
        Base.metadata.drop_all(bind=engine)


def test_list_and_get_are_strictly_read_only_for_unlinked_items(db_session: Session) -> None:
    product = Product(name="Marine Collagen 300g", category="Supplements", domain="01-health")
    db_session.add(product)
    db_session.flush()
    unlinked = _add_receipt_with_item(
        db_session,
        receipt_date=date(2026, 9, 5),
        merchant="Harbour Pharmacy",
        item_name="Marine Collagen 300g",
        unit_price=42.00,
        total_price=42.00,
        product_id=None,
        domain="01-health",
    )
    db_session.commit()

    listed = list_products(db_session)
    detail = get_product_snapshot(db_session, product.id)

    assert listed.total_count == 1
    assert listed.products[0].purchase_count == 0
    assert detail.purchase_count == 0
    assert detail.price_history == ()

    db_session.expire_all()
    reloaded = db_session.get(ReceiptItem, unlinked.id)
    assert reloaded is not None
    assert reloaded.product_id is None


def test_backfill_unlinked_receipt_item_products_is_idempotent(db_session: Session) -> None:
    target_product = Product(name="Daily Electrolytes", category="Supplements")
    other_product = Product(name="Zinc Picolinate", category="Supplements")
    db_session.add_all([target_product, other_product])
    db_session.flush()

    item_lower = _add_receipt_with_item(
        db_session,
        receipt_date=date(2026, 8, 1),
        merchant="North Pharmacy",
        item_name="daily electrolytes",
        unit_price=24.00,
        total_price=24.00,
    )
    item_padded = _add_receipt_with_item(
        db_session,
        receipt_date=date(2026, 9, 1),
        merchant="North Pharmacy",
        item_name="  Daily Electrolytes  ",
        unit_price=26.00,
        total_price=26.00,
    )
    item_unmatched = _add_receipt_with_item(
        db_session,
        receipt_date=date(2026, 9, 2),
        merchant="Home Goods",
        item_name="Unmatched Bath Towel",
        unit_price=19.00,
        total_price=19.00,
    )
    item_linked = _add_receipt_with_item(
        db_session,
        receipt_date=date(2026, 9, 3),
        merchant="North Pharmacy",
        item_name="Zinc Picolinate",
        unit_price=18.00,
        total_price=18.00,
        product_id=other_product.id,
    )
    db_session.commit()

    first_run = backfill_unlinked_receipt_item_products(db_session)
    second_run = backfill_unlinked_receipt_item_products(db_session)

    assert first_run == 2
    assert second_run == 0
    db_session.expire_all()
    assert db_session.get(ReceiptItem, item_lower.id).product_id == target_product.id
    assert db_session.get(ReceiptItem, item_padded.id).product_id == target_product.id
    assert db_session.get(ReceiptItem, item_unmatched.id).product_id is None
    assert db_session.get(ReceiptItem, item_linked.id).product_id == other_product.id

    snapshot = get_product_snapshot(db_session, target_product.id)
    assert snapshot.purchase_count == 2
    assert snapshot.price_change_latest == 2.00


def test_write_time_linking_on_create_and_get_or_create(db_session: Session) -> None:
    preexisting_one = _add_receipt_with_item(
        db_session,
        receipt_date=date(2026, 8, 10),
        merchant="Peak Nutrition",
        item_name="Creatine Monohydrate 500g",
        unit_price=39.00,
        total_price=39.00,
    )
    preexisting_two = _add_receipt_with_item(
        db_session,
        receipt_date=date(2026, 8, 12),
        merchant="Peak Nutrition",
        item_name="  magnesium complex ",
        unit_price=28.00,
        total_price=28.00,
    )
    db_session.commit()

    created = create_product(
        db_session,
        ProductCreateInput(name="Creatine Monohydrate 500g", domain="01-health"),
        tag_resolver=lambda _name, _dom: ["supplements", "creatine"],
    )
    assert created.purchase_count == 1
    assert created.tags == ("supplements", "creatine")
    assert db_session.get(ReceiptItem, preexisting_one.id).product_id == created.id

    prod = get_or_create_product(
        db_session,
        "Magnesium Complex",
        category="Supplements",
        domain="01-health",
        tag_resolver=lambda _name, _dom: ["magnesium"],
    )
    assert db_session.get(ReceiptItem, preexisting_two.id).product_id == prod.id

    merged = get_or_create_product(
        db_session,
        "magnesium complex",
        brand="Harbour Labs",
        tags=["sleep"],
        volume_ml="90 caps",
    )
    db_session.commit()
    assert merged.id == prod.id
    snapshot = get_product_snapshot(db_session, prod.id)
    assert snapshot.brand == "Harbour Labs"
    assert snapshot.volume_ml == "90 caps"
    assert snapshot.tags == ("magnesium", "sleep")


def test_list_products_batched_queries_and_filters(db_session: Session) -> None:
    for idx in range(10):
        created = create_product(
            db_session,
            ProductCreateInput(
                name=f"Catalog Item {idx:02d}",
                brand="Nordic Labs" if idx % 2 == 0 else "Harbour Co",
                category="Supplements",
                domain="01-health" if idx < 5 else "02-skin",
                tags=("omega",) if idx % 2 == 0 else ("serum",),
                notes="morning routine" if idx == 0 else None,
                is_favorite=(idx == 0),
            ),
        )
        _add_receipt_with_item(
            db_session,
            receipt_date=date(2026, 8, 1),
            merchant="Shop A",
            item_name=created.name,
            unit_price=20.0 + idx,
            total_price=20.0 + idx,
            product_id=created.id,
        )
        _add_receipt_with_item(
            db_session,
            receipt_date=date(2026, 9, 1),
            merchant="Shop B",
            item_name=created.name,
            unit_price=22.0 + idx,
            total_price=20.0 + idx,
            product_id=created.id,
            discount_amount=2.0,
        )
    db_session.commit()

    sql_statements: list[str] = []

    def _count_queries(_conn, _cursor, statement, _params, _context, _executemany):  # type: ignore[no-untyped-def]
        sql_statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", _count_queries)
    try:
        result = list_products(db_session, ProductListQuery(limit=50, offset=0))
    finally:
        event.remove(engine, "before_cursor_execute", _count_queries)

    assert len(sql_statements) <= 4
    assert result.total_count == 10
    assert len(result.products) == 10
    assert result.all_tags == ("omega", "serum")
    first = result.products[0]
    assert first.is_favorite is True
    assert first.purchase_count == 2
    assert first.price_change_latest == 2.0
    assert first.lowest_price == 20.0
    assert first.highest_price == 22.0
    assert first.average_price == 21.0
    assert first.avg_days_between_purchases == 31.0
    assert first.estimated_depletion_date == date(2026, 10, 2)
    assert first.price_history[1].discount_amount == 2.0

    favs = list_products(db_session, ProductListQuery(is_favorite=True))
    assert favs.total_count == 1
    skin_page = list_products(db_session, ProductListQuery(domain="02-skin", limit=2, offset=1))
    assert skin_page.total_count == 5
    assert len(skin_page.products) == 2
    searched = list_products(db_session, ProductListQuery(search="morning", tag="omega"))
    assert searched.total_count == 1


def test_compute_product_statistics_batched_and_filtered(db_session: Session) -> None:
    _add_receipt_with_item(
        db_session,
        receipt_date=date(2026, 8, 1),
        merchant="Chemist Alpha",
        item_name="Ascorbic Serum 30ml",
        unit_price=30.0,
        total_price=30.0,
        domain="02-skin",
        product_type="vitamin_c_serum",
    )
    _add_receipt_with_item(
        db_session,
        receipt_date=date(2026, 9, 1),
        merchant="Chemist Beta",
        item_name=" Ascorbic Serum 30ml ",
        unit_price=35.0,
        total_price=35.0,
        domain="02-skin",
        product_type="vitamin_c_serum",
    )
    _add_receipt_with_item(
        db_session,
        receipt_date=date(2026, 9, 5),
        merchant="Fuel Depot",
        item_name="Whey Blend 1kg",
        unit_price=80.0,
        total_price=80.0,
        domain="01-health",
        product_type="protein_powder",
    )
    db_session.commit()

    sql_statements: list[str] = []

    def _count_queries(_conn, _cursor, statement, _params, _context, _executemany):  # type: ignore[no-untyped-def]
        sql_statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", _count_queries)
    try:
        all_stats = compute_product_statistics(db_session)
        skin_stats = compute_product_statistics(
            db_session, domain="02-skin", product_type="vitamin_c_serum"
        )
    finally:
        event.remove(engine, "before_cursor_execute", _count_queries)

    assert len(sql_statements) <= 2
    assert all_stats.total_products == 2
    assert all_stats.total_spend == 145.0
    assert all_stats.products[0].item_name == "Whey Blend 1kg"
    assert skin_stats.total_products == 1
    assert skin_stats.products[0].purchase_count == 2
    assert skin_stats.products[0].avg_days_between_purchases == 31.0
    assert skin_stats.products[0].estimated_depletion_date == date(2026, 10, 2)


def test_update_unset_vs_none_and_domain_errors(db_session: Session) -> None:
    created = create_product(
        db_session,
        ProductCreateInput(
            name="Daily Oats",
            brand="Harbour Mill",
            domain="general",
            volume_ml="1kg",
            notes="Original note",
            rating=5,
        ),
    )
    create_product(db_session, ProductCreateInput(name="Other Grain"))

    partial = update_product(
        db_session,
        created.id,
        ProductUpdateInput(name="daily oats", notes=None, rating=None),
    )
    assert partial.name == "daily oats"
    assert partial.brand == "Harbour Mill"
    assert partial.notes is None
    assert partial.rating is None

    with pytest.raises(ProductNotFoundError):
        get_product_snapshot(db_session, 99999)
    with pytest.raises(ProductNotFoundError):
        update_product(db_session, 99999, ProductUpdateInput(rating=4))
    with pytest.raises(ProductNotFoundError):
        delete_product(db_session, 99999)
    with pytest.raises(ProductConflictError):
        create_product(db_session, ProductCreateInput(name="DAILY OATS"))
    with pytest.raises(ProductConflictError):
        update_product(db_session, created.id, ProductUpdateInput(name="other grain"))
    with pytest.raises(ProductValidationError):
        create_product(db_session, ProductCreateInput(name="   "))
    with pytest.raises(ProductValidationError):
        create_product(db_session, ProductCreateInput(name="X" * 201))
    with pytest.raises(ProductValidationError):
        create_product(db_session, ProductCreateInput(name="Bad Rating", rating=6))
    with pytest.raises(ProductValidationError):
        update_product(db_session, created.id, ProductUpdateInput(category="   "))
    with pytest.raises(ProductValidationError):
        list_products(db_session, ProductListQuery(limit=0))
    with pytest.raises(ProductValidationError):
        list_products(db_session, ProductListQuery(offset=-1))


def test_delete_product_unlinks_receipt_items_with_fk_enforcement() -> None:
    session_factory, engine = _new_memory_session_factory(enable_fk=True)
    try:
        with session_factory() as db:
            fk_state = db.execute(text("PRAGMA foreign_keys")).scalar()
            assert fk_state == 1
            created = create_product(db, ProductCreateInput(name="Hydration Mist 100ml"))
            item_a = _add_receipt_with_item(
                db,
                receipt_date=date(2026, 9, 10),
                merchant="City Pharmacy",
                item_name="Hydration Mist 100ml",
                unit_price=19.50,
                total_price=19.50,
                product_id=created.id,
            )
            item_b = _add_receipt_with_item(
                db,
                receipt_date=date(2026, 9, 20),
                merchant="City Pharmacy",
                item_name="Hydration Mist 100ml",
                unit_price=21.00,
                total_price=21.00,
                product_id=created.id,
            )
            db.commit()

            deleted_id = delete_product(db, created.id)
            assert deleted_id == created.id
            with pytest.raises(ProductNotFoundError):
                get_product_snapshot(db, created.id)

            db.expire_all()
            reloaded_a = db.get(ReceiptItem, item_a.id)
            reloaded_b = db.get(ReceiptItem, item_b.id)
            assert reloaded_a is not None and reloaded_a.product_id is None
            assert reloaded_b is not None and reloaded_b.product_id is None
    finally:
        Base.metadata.drop_all(bind=engine)
