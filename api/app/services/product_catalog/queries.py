"""Strictly read-only batched catalog queries and price telemetry calculations."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.models import Product, Receipt, ReceiptItem
from app.services.product_catalog._types import (
    _MAX_LIST_LIMIT,
    PricePointSnapshot,
    ProductListQuery,
    ProductListResult,
    ProductNotFoundError,
    ProductSnapshot,
    ProductStatisticSnapshot,
    ProductStatisticsSnapshot,
    ProductValidationError,
    parse_product_tags,
)


def list_products(
    db: Session, query: ProductListQuery = ProductListQuery()
) -> ProductListResult:
    """List paginated catalog products with batched price history in O(1) queries."""
    _validate_list_query(query)
    all_tags = _collect_all_product_tags(db)
    stmt = _build_product_filter_stmt(query)
    total_count = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    products = list(
        db.scalars(
            stmt.order_by(
                Product.is_favorite.desc(), Product.updated_at.desc(), Product.name.asc()
            )
            .offset(query.offset)
            .limit(query.limit)
        ).all()
    )
    items_by_product = _load_items_by_product(db, [p.id for p in products])
    snapshots = tuple(
        _build_product_snapshot(product, items_by_product.get(product.id, ()))
        for product in products
    )
    return ProductListResult(total_count=total_count, all_tags=all_tags, products=snapshots)


def get_product_snapshot(db: Session, product_id: int) -> ProductSnapshot:
    """Return a detached ProductSnapshot with full price telemetry or raise not found."""
    product = db.get(Product, product_id)
    if product is None:
        raise ProductNotFoundError(f"Product {product_id} not found")
    items_by_product = _load_items_by_product(db, [product.id])
    return _build_product_snapshot(product, items_by_product.get(product.id, ()))


def compute_product_statistics(
    db: Session,
    *,
    domain: Optional[str] = None,
    product_type: Optional[str] = None,
) -> ProductStatisticsSnapshot:
    """Aggregate product spend and replenishment cadence across receipt line items."""
    stmt = select(ReceiptItem).options(joinedload(ReceiptItem.receipt)).join(Receipt)
    if domain:
        stmt = stmt.where(ReceiptItem.domain == domain)
    if product_type:
        stmt = stmt.where(ReceiptItem.product_type == product_type)
    items = list(
        db.scalars(stmt.order_by(Receipt.receipt_date.asc(), ReceiptItem.id.asc())).all()
    )

    products_map: dict[str, list[tuple[ReceiptItem, date]]] = {}
    total_spend = 0.0
    for item in items:
        key = item.item_name.strip()
        products_map.setdefault(key, []).append((item, item.receipt.receipt_date))
        total_spend += float(item.total_price)

    stats = [
        _build_product_statistic(item_name, occurrences)
        for item_name, occurrences in products_map.items()
    ]
    stats.sort(key=lambda entry: entry.total_spent, reverse=True)
    return ProductStatisticsSnapshot(
        domain=domain,
        total_products=len(stats),
        total_spend=total_spend,
        products=tuple(stats),
    )


def _validate_list_query(query: ProductListQuery) -> None:
    if query.limit < 1 or query.limit > _MAX_LIST_LIMIT:
        raise ProductValidationError(
            f"Product list limit must be between 1 and {_MAX_LIST_LIMIT}."
        )
    if query.offset < 0:
        raise ProductValidationError("Product list offset must be zero or positive.")


def _collect_all_product_tags(db: Session) -> tuple[str, ...]:
    raw_tag_rows = db.scalars(select(Product.tags).where(Product.tags.is_not(None))).all()
    distinct_tags: set[str] = set()
    for raw_tags in raw_tag_rows:
        for tag in parse_product_tags(raw_tags):
            cleaned = tag.strip().lower()
            if cleaned:
                distinct_tags.add(cleaned)
    return tuple(sorted(distinct_tags))


def _build_product_filter_stmt(query: ProductListQuery) -> Select[tuple[Product]]:
    stmt = select(Product)
    if query.domain:
        stmt = stmt.where(Product.domain == query.domain)
    if query.is_favorite is not None:
        stmt = stmt.where(Product.is_favorite == query.is_favorite)
    if query.search and query.search.strip():
        pattern = f"%{query.search.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Product.name).like(pattern),
                func.lower(Product.brand).like(pattern),
                func.lower(Product.notes).like(pattern),
                func.lower(Product.tags).like(pattern),
            )
        )
    if query.tag and query.tag.strip():
        tag_pattern = f"%{query.tag.strip().lower()}%"
        stmt = stmt.where(func.lower(Product.tags).like(tag_pattern))
    return stmt


def _load_items_by_product(
    db: Session, product_ids: Sequence[int]
) -> dict[int, list[ReceiptItem]]:
    if not product_ids:
        return {}
    stmt = (
        select(ReceiptItem)
        .options(joinedload(ReceiptItem.receipt))
        .join(Receipt)
        .where(ReceiptItem.product_id.in_(product_ids))
        .order_by(Receipt.receipt_date.asc(), ReceiptItem.id.asc())
    )
    grouped: dict[int, list[ReceiptItem]] = {pid: [] for pid in product_ids}
    for item in db.scalars(stmt).all():
        if item.product_id is not None:
            grouped.setdefault(item.product_id, []).append(item)
    return grouped


def _to_price_point_snapshot(item: ReceiptItem) -> PricePointSnapshot:
    return PricePointSnapshot(
        date=item.receipt.receipt_date,
        merchant=item.receipt.merchant,
        unit_price=float(item.unit_price),
        discount_amount=(
            float(item.discount_amount) if item.discount_amount is not None else None
        ),
        total_price=float(item.total_price),
        quantity=float(item.quantity),
        receipt_id=item.receipt_id,
        receipt_item_id=item.id,
    )


def _compute_depletion_metrics(
    dates: Sequence[date],
) -> tuple[Optional[date], Optional[date], Optional[float], Optional[date]]:
    if not dates:
        return None, None, None, None
    first_purchased = dates[0]
    last_purchased = dates[-1]
    avg_days: Optional[float] = None
    depletion_date: Optional[date] = None
    if len(dates) > 1:
        total_days = (last_purchased - first_purchased).days
        avg_days = round(total_days / (len(dates) - 1), 1)
        if avg_days > 0:
            depletion_date = last_purchased + timedelta(days=int(avg_days))
    return first_purchased, last_purchased, avg_days, depletion_date


def _build_product_snapshot(
    product: Product, items: Sequence[ReceiptItem]
) -> ProductSnapshot:
    price_history = tuple(_to_price_point_snapshot(item) for item in items)
    unit_prices = [point.unit_price for point in price_history]
    total_spent = sum(point.total_price for point in price_history)
    first_purchased, last_purchased, avg_days, depletion_date = _compute_depletion_metrics(
        [point.date for point in price_history]
    )
    price_change_latest = (
        round(unit_prices[-1] - unit_prices[-2], 2) if len(unit_prices) >= 2 else None
    )
    return ProductSnapshot(
        id=product.id,
        name=product.name,
        created_at=product.created_at,
        updated_at=product.updated_at,
        brand=product.brand,
        category=product.category,
        domain=product.domain,
        tags=parse_product_tags(product.tags),
        volume_ml=product.volume_ml,
        notes=product.notes,
        rating=product.rating,
        is_favorite=bool(product.is_favorite),
        purchase_count=len(price_history),
        total_spent=round(total_spent, 2),
        latest_price=unit_prices[-1] if unit_prices else None,
        lowest_price=min(unit_prices) if unit_prices else None,
        highest_price=max(unit_prices) if unit_prices else None,
        average_price=round(sum(unit_prices) / len(unit_prices), 2) if unit_prices else None,
        price_change_latest=price_change_latest,
        first_purchased=first_purchased,
        last_purchased=last_purchased,
        avg_days_between_purchases=avg_days,
        estimated_depletion_date=depletion_date,
        price_history=price_history,
    )


def _build_product_statistic(
    item_name: str, occurrences: list[tuple[ReceiptItem, date]]
) -> ProductStatisticSnapshot:
    occurrences.sort(key=lambda entry: entry[1])
    latest_item, _ = occurrences[-1]
    first_date, last_date, avg_days, depletion_date = _compute_depletion_metrics(
        [entry_date for _, entry_date in occurrences]
    )
    assert first_date is not None and last_date is not None
    return ProductStatisticSnapshot(
        item_name=item_name,
        brand=latest_item.brand,
        category=latest_item.category,
        domain=latest_item.domain,
        product_type=latest_item.product_type,
        volume_ml=latest_item.volume_ml,
        purchase_count=len(occurrences),
        total_spent=sum(float(item.total_price) for item, _ in occurrences),
        unit_price_latest=float(latest_item.unit_price),
        first_purchased=first_date,
        last_purchased=last_date,
        avg_days_between_purchases=avg_days,
        estimated_depletion_date=depletion_date,
    )
