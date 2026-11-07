"""Router for product catalog, tag management, user notes, and historical price telemetry."""

import json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Product
from app.schemas import (
    ProductCreate,
    ProductListResponse,
    ProductRead,
    ProductUpdate,
)
from app.services.receipt_service import (
    build_product_read,
    extract_tags_for_line_item,
)

router = APIRouter(prefix="/products", tags=["Product Catalog & Price Tracking"])


@router.get("", response_model=ProductListResponse)
def list_products(
    domain: Optional[str] = Query(default=None, description="Filter by domain e.g. '02-skin', '01-health'"),
    tag: Optional[str] = Query(default=None, description="Filter by tag keyword"),
    search: Optional[str] = Query(default=None, description="Search across product name, brand, notes, or tags"),
    is_favorite: Optional[bool] = Query(default=None, description="Filter favorites"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """List products with price statistics, historical price change trends, and tag indexing."""
    # 1. Collect all distinct tags across all products for UI filtering
    all_tag_strings = db.scalars(select(Product.tags).where(Product.tags.is_not(None))).all()
    all_tags_set: set[str] = set()
    for tag_str in all_tag_strings:
        if not tag_str:
            continue
        try:
            parsed = json.loads(tag_str)
            if isinstance(parsed, list):
                all_tags_set.update([t.strip().lower() for t in parsed if isinstance(t, str)])
        except Exception:
            for t in tag_str.split(","):
                if t.strip():
                    all_tags_set.add(t.strip().lower())

    # 2. Build filtered query
    query = select(Product)
    if domain:
        query = query.where(Product.domain == domain)
    if is_favorite is not None:
        query = query.where(Product.is_favorite == is_favorite)
    if search:
        pattern = f"%{search.strip().lower()}%"
        query = query.where(
            or_(
                func.lower(Product.name).like(pattern),
                func.lower(Product.brand).like(pattern),
                func.lower(Product.notes).like(pattern),
                func.lower(Product.tags).like(pattern),
            )
        )
    if tag:
        tag_pattern = f"%{tag.strip().lower()}%"
        query = query.where(func.lower(Product.tags).like(tag_pattern))

    total_count = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    products = db.scalars(
        query.order_by(Product.is_favorite.desc(), Product.updated_at.desc(), Product.name.asc())
        .offset(offset)
        .limit(limit)
    ).all()

    products_read = [build_product_read(db, p) for p in products]

    return ProductListResponse(
        total_count=total_count,
        all_tags=sorted(list(all_tags_set)),
        products=products_read,
    )


@router.get("/{product_id}", response_model=ProductRead)
def get_product(
    product_id: int,
    db: Session = Depends(get_db),
):
    """Retrieve full product detail with complete historical price points and notes."""
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product {product_id} not found",
        )
    return build_product_read(db, product)


@router.post("", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
def create_product(
    payload: ProductCreate,
    db: Session = Depends(get_db),
):
    """Create a new product in the catalog with custom tags and notes."""
    existing = db.scalars(
        select(Product).where(func.lower(Product.name) == payload.name.strip().lower())
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Product with name '{payload.name.strip()}' already exists (ID {existing.id})",
        )

    tags = payload.tags
    if not tags:
        tags = extract_tags_for_line_item(payload.name, payload.domain)

    product = Product(
        name=payload.name.strip(),
        brand=payload.brand.strip() if payload.brand else None,
        category=payload.category,
        domain=payload.domain,
        tags=json.dumps(tags),
        volume_ml=payload.volume_ml.strip() if payload.volume_ml else None,
        notes=payload.notes.strip() if payload.notes else None,
        rating=payload.rating,
        is_favorite=payload.is_favorite,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return build_product_read(db, product)


@router.patch("/{product_id}", response_model=ProductRead)
def update_product(
    product_id: int,
    payload: ProductUpdate,
    db: Session = Depends(get_db),
):
    """Update product metadata, tags, notes, rating, or favorite status."""
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product {product_id} not found",
        )

    if payload.name is not None and payload.name.strip() != product.name:
        existing = db.scalars(
            select(Product).where(
                func.lower(Product.name) == payload.name.strip().lower(),
                Product.id != product_id,
            )
        ).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Product with name '{payload.name.strip()}' already exists",
            )
        product.name = payload.name.strip()

    if payload.brand is not None:
        product.brand = payload.brand.strip() if payload.brand else None
    if payload.category is not None:
        product.category = payload.category
    if payload.domain is not None:
        product.domain = payload.domain
    if payload.tags is not None:
        product.tags = json.dumps(payload.tags)
    if payload.volume_ml is not None:
        product.volume_ml = payload.volume_ml.strip() if payload.volume_ml else None
    if payload.notes is not None:
        product.notes = payload.notes.strip() if payload.notes else None
    if payload.rating is not None:
        product.rating = payload.rating
    if payload.is_favorite is not None:
        product.is_favorite = payload.is_favorite

    db.commit()
    db.refresh(product)
    return build_product_read(db, product)


@router.delete("/{product_id}", status_code=status.HTTP_200_OK)
def delete_product(
    product_id: int,
    db: Session = Depends(get_db),
):
    """Delete a product from the catalog. Associated receipt items will be unlinked."""
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product {product_id} not found",
        )

    db.delete(product)
    db.commit()
    return {"status": "deleted", "id": product_id}
