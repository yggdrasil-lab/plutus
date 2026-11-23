"""Thin HTTP router for product catalog, tag management, notes, and historical price telemetry."""

from typing import NoReturn, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import (
    PricePoint,
    ProductCreate,
    ProductListResponse,
    ProductRead,
    ProductUpdate,
)
from app.services.product_catalog import (
    UNSET,
    PricePointSnapshot,
    ProductCatalogError,
    ProductConflictError,
    ProductCreateInput,
    ProductListQuery,
    ProductNotFoundError,
    ProductSnapshot,
    ProductUpdateInput,
    ProductValidationError,
    create_product as create_product_record,
    delete_product as delete_product_record,
    get_product_snapshot,
    list_products as list_product_snapshots,
    update_product as update_product_record,
)
from app.services.receipt_service import extract_tags_for_line_item

router = APIRouter(prefix="/products", tags=["Product Catalog & Price Tracking"])

_EXCEPTION_STATUS_MAP: tuple[tuple[type[ProductCatalogError], int], ...] = (
    (ProductNotFoundError, status.HTTP_404_NOT_FOUND),
    (ProductConflictError, status.HTTP_400_BAD_REQUEST),
    (ProductValidationError, status.HTTP_400_BAD_REQUEST),
)


def _raise_http_error(exc: ProductCatalogError) -> NoReturn:
    for exc_type, http_status in _EXCEPTION_STATUS_MAP:
        if isinstance(exc, exc_type):
            raise HTTPException(status_code=http_status, detail=str(exc)) from exc
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _to_price_point(point: PricePointSnapshot) -> PricePoint:
    return PricePoint(
        date=point.date,
        merchant=point.merchant,
        unit_price=point.unit_price,
        discount_amount=point.discount_amount,
        total_price=point.total_price,
        quantity=point.quantity,
        receipt_id=point.receipt_id,
        receipt_item_id=point.receipt_item_id,
    )


def _to_product_read(snapshot: ProductSnapshot) -> ProductRead:
    return ProductRead(
        id=snapshot.id,
        name=snapshot.name,
        brand=snapshot.brand,
        category=snapshot.category,
        domain=snapshot.domain,
        tags=list(snapshot.tags),
        volume_ml=snapshot.volume_ml,
        notes=snapshot.notes,
        rating=snapshot.rating,
        is_favorite=snapshot.is_favorite,
        purchase_count=snapshot.purchase_count,
        total_spent=snapshot.total_spent,
        latest_price=snapshot.latest_price,
        lowest_price=snapshot.lowest_price,
        highest_price=snapshot.highest_price,
        average_price=snapshot.average_price,
        price_change_latest=snapshot.price_change_latest,
        first_purchased=snapshot.first_purchased,
        last_purchased=snapshot.last_purchased,
        avg_days_between_purchases=snapshot.avg_days_between_purchases,
        estimated_depletion_date=snapshot.estimated_depletion_date,
        price_history=[_to_price_point(point) for point in snapshot.price_history],
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
    )


@router.get("", response_model=ProductListResponse)
def list_products(
    domain: Optional[str] = Query(
        default=None, description="Filter by domain e.g. '02-skin', '01-health'"
    ),
    tag: Optional[str] = Query(default=None, description="Filter by tag keyword"),
    search: Optional[str] = Query(
        default=None, description="Search across product name, brand, notes, or tags"
    ),
    is_favorite: Optional[bool] = Query(default=None, description="Filter favorites"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """List products with price statistics, historical price change trends, and tag indexing."""
    query = ProductListQuery(
        domain=domain,
        tag=tag,
        search=search,
        is_favorite=is_favorite,
        limit=limit,
        offset=offset,
    )
    try:
        result = list_product_snapshots(db, query)
    except ProductCatalogError as exc:
        _raise_http_error(exc)
    return ProductListResponse(
        total_count=result.total_count,
        all_tags=list(result.all_tags),
        products=[_to_product_read(item) for item in result.products],
    )


@router.get("/{product_id}", response_model=ProductRead)
def get_product(
    product_id: int,
    db: Session = Depends(get_db),
):
    """Retrieve full product detail with complete historical price points and notes."""
    try:
        snapshot = get_product_snapshot(db, product_id)
    except ProductCatalogError as exc:
        _raise_http_error(exc)
    return _to_product_read(snapshot)


@router.post("", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
def create_product(
    payload: ProductCreate,
    db: Session = Depends(get_db),
):
    """Create a new product in the catalog with custom tags and notes."""
    command = ProductCreateInput(
        name=payload.name,
        brand=payload.brand,
        category=payload.category,
        domain=payload.domain,
        tags=payload.tags,
        volume_ml=payload.volume_ml,
        notes=payload.notes,
        rating=payload.rating,
        is_favorite=payload.is_favorite,
    )
    try:
        snapshot = create_product_record(
            db, command, tag_resolver=extract_tags_for_line_item
        )
    except ProductCatalogError as exc:
        _raise_http_error(exc)
    return _to_product_read(snapshot)


@router.patch("/{product_id}", response_model=ProductRead)
def update_product(
    product_id: int,
    payload: ProductUpdate,
    db: Session = Depends(get_db),
):
    """Update product metadata, tags, notes, rating, or favorite status."""
    fields = payload.model_fields_set
    command = ProductUpdateInput(
        name=payload.name if "name" in fields else None,
        brand=payload.brand if "brand" in fields else UNSET,
        category=payload.category if "category" in fields else None,
        domain=payload.domain if "domain" in fields else UNSET,
        tags=payload.tags if "tags" in fields else None,
        volume_ml=payload.volume_ml if "volume_ml" in fields else UNSET,
        notes=payload.notes if "notes" in fields else UNSET,
        rating=payload.rating if "rating" in fields else UNSET,
        is_favorite=payload.is_favorite if "is_favorite" in fields else None,
    )
    try:
        snapshot = update_product_record(db, product_id, command)
    except ProductCatalogError as exc:
        _raise_http_error(exc)
    return _to_product_read(snapshot)


@router.delete("/{product_id}", status_code=status.HTTP_200_OK)
def delete_product(
    product_id: int,
    db: Session = Depends(get_db),
):
    """Delete a product from the catalog. Associated receipt items will be unlinked."""
    try:
        deleted_id = delete_product_record(db, product_id)
    except ProductCatalogError as exc:
        _raise_http_error(exc)
    return {"status": "deleted", "id": deleted_id}
