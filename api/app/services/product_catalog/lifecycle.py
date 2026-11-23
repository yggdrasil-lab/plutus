"""Product catalog CRUD, write-time receipt item linking, and startup backfill."""

from __future__ import annotations

from collections.abc import Sequence
import json
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Product, ReceiptItem
from app.services.product_catalog._types import (
    _MAX_BRAND_LENGTH,
    _MAX_DOMAIN_LENGTH,
    _MAX_VOLUME_LENGTH,
    UNSET,
    ProductConflictError,
    ProductCreateInput,
    ProductNotFoundError,
    ProductSnapshot,
    ProductUpdateInput,
    TagResolver,
    normalize_product_name,
    parse_product_tags,
    validate_category_name,
    validate_optional_text,
    validate_rating,
)
from app.services.product_catalog.queries import get_product_snapshot


def backfill_unlinked_receipt_item_products(db: Session) -> int:
    """Idempotently link unlinked receipt items whose name matches an existing product."""
    product_rows = db.execute(
        select(Product.id, Product.name).order_by(Product.id.asc())
    ).all()
    if not product_rows:
        return 0

    by_lower_name: dict[str, int] = {}
    for product_id, name in product_rows:
        key = (name or "").strip().lower()
        if key:
            by_lower_name.setdefault(key, int(product_id))

    unlinked_items = db.scalars(
        select(ReceiptItem).where(ReceiptItem.product_id.is_(None))
    ).all()
    updated = 0
    for item in unlinked_items:
        matched_id = by_lower_name.get((item.item_name or "").strip().lower())
        if matched_id is not None:
            item.product_id = matched_id
            updated += 1
    if updated > 0:
        db.commit()
    return updated


def get_or_create_product(
    db: Session,
    item_name: str,
    *,
    brand: Optional[str] = None,
    category: Optional[str] = None,
    domain: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
    volume_ml: Optional[str] = None,
    tag_resolver: Optional[TagResolver] = None,
) -> Product:
    """Upsert a catalog product by name, merge metadata, and link unlinked items."""
    canonical = normalize_product_name(item_name)
    product = db.scalars(
        select(Product).where(func.lower(func.trim(Product.name)) == canonical.lower())
    ).first()
    tags_list = _resolve_tags(canonical, domain, tags, tag_resolver)

    if product is not None:
        _merge_existing_product(
            product, tags_list=tags_list, brand=brand, volume_ml=volume_ml
        )
    else:
        product = Product(
            name=canonical,
            brand=_clip_optional(brand, _MAX_BRAND_LENGTH),
            category=((category or "").strip()[:50] or "General"),
            domain=_clip_optional(domain, _MAX_DOMAIN_LENGTH),
            tags=json.dumps(tags_list),
            volume_ml=_clip_optional(volume_ml, _MAX_VOLUME_LENGTH),
        )
        db.add(product)
    db.flush()

    linked = _link_matching_unlinked_receipt_items(
        db, product_id=product.id, canonical_name=canonical
    )
    if linked > 0:
        db.flush()
    return product


def create_product(
    db: Session,
    payload: ProductCreateInput,
    *,
    tag_resolver: Optional[TagResolver] = None,
) -> ProductSnapshot:
    """Create a new catalog product, link matching unlinked items, and return snapshot."""
    canonical = normalize_product_name(payload.name)
    existing = db.scalars(
        select(Product).where(func.lower(func.trim(Product.name)) == canonical.lower())
    ).first()
    if existing is not None:
        raise ProductConflictError(
            f"Product with name '{canonical}' already exists (ID {existing.id})"
        )

    product = _build_new_product(canonical, payload, tag_resolver)
    db.add(product)
    db.flush()
    _link_matching_unlinked_receipt_items(
        db, product_id=product.id, canonical_name=canonical
    )
    db.commit()
    return get_product_snapshot(db, product.id)


def update_product(
    db: Session, product_id: int, payload: ProductUpdateInput
) -> ProductSnapshot:
    """Apply validated partial updates to an existing product and return snapshot."""
    product = db.get(Product, product_id)
    if product is None:
        raise ProductNotFoundError(f"Product {product_id} not found")

    if payload.name is not None:
        product.name = _resolve_updated_name(db, product, payload.name)

    _apply_nullable_updates(product, payload)
    _apply_required_updates(product, payload)
    db.commit()
    return get_product_snapshot(db, product.id)


def delete_product(db: Session, product_id: int) -> int:
    """Unlink associated receipt items and delete a product from the catalog."""
    product = db.get(Product, product_id)
    if product is None:
        raise ProductNotFoundError(f"Product {product_id} not found")

    linked_items = list(
        db.scalars(select(ReceiptItem).where(ReceiptItem.product_id == product_id)).all()
    )
    for item in linked_items:
        item.product_id = None
    if linked_items:
        db.flush()

    db.delete(product)
    db.commit()
    return product_id


def _build_new_product(
    canonical: str,
    payload: ProductCreateInput,
    tag_resolver: Optional[TagResolver],
) -> Product:
    domain = validate_optional_text(
        payload.domain, max_length=_MAX_DOMAIN_LENGTH, field_name="domain"
    )
    tags_list = _resolve_tags(canonical, domain, payload.tags, tag_resolver)
    return Product(
        name=canonical,
        brand=validate_optional_text(
            payload.brand, max_length=_MAX_BRAND_LENGTH, field_name="brand"
        ),
        category=validate_category_name(payload.category, allow_default=True),
        domain=domain,
        tags=json.dumps(tags_list),
        volume_ml=validate_optional_text(
            payload.volume_ml, max_length=_MAX_VOLUME_LENGTH, field_name="volume_ml"
        ),
        notes=validate_optional_text(payload.notes, max_length=None, field_name="notes"),
        rating=validate_rating(payload.rating),
        is_favorite=bool(payload.is_favorite),
    )


def _resolve_updated_name(db: Session, product: Product, raw_name: str) -> str:
    canonical = normalize_product_name(raw_name)
    if canonical == product.name:
        return canonical
    conflict = db.scalars(
        select(Product).where(
            func.lower(func.trim(Product.name)) == canonical.lower(),
            Product.id != product.id,
        )
    ).first()
    if conflict is not None:
        raise ProductConflictError(f"Product with name '{canonical}' already exists")
    return canonical


def _link_matching_unlinked_receipt_items(
    db: Session, *, product_id: int, canonical_name: str
) -> int:
    target_lower = canonical_name.strip().lower()
    unlinked = list(
        db.scalars(
            select(ReceiptItem).where(
                ReceiptItem.product_id.is_(None),
                func.lower(func.trim(ReceiptItem.item_name)) == target_lower,
            )
        ).all()
    )
    for item in unlinked:
        item.product_id = product_id
    return len(unlinked)


def _resolve_tags(
    item_name: str,
    domain: Optional[str],
    tags: Optional[Sequence[str]],
    tag_resolver: Optional[TagResolver],
) -> list[str]:
    tags_list = [str(tag) for tag in tags] if tags else []
    if not tags_list and tag_resolver is not None:
        tags_list = [str(tag) for tag in tag_resolver(item_name, domain)]
    return tags_list


def _clip_optional(value: Optional[str], max_length: int) -> Optional[str]:
    if not value:
        return None
    cleaned = value.strip()
    return cleaned[:max_length] if cleaned else None


def _merge_existing_product(
    product: Product,
    *,
    tags_list: list[str],
    brand: Optional[str],
    volume_ml: Optional[str],
) -> Product:
    existing_tags = list(parse_product_tags(product.tags))
    combined_tags = sorted(set(existing_tags + tags_list))
    product.tags = json.dumps(combined_tags)
    if not product.brand:
        product.brand = _clip_optional(brand, _MAX_BRAND_LENGTH)
    if not product.volume_ml:
        product.volume_ml = _clip_optional(volume_ml, _MAX_VOLUME_LENGTH)
    return product


def _apply_nullable_updates(product: Product, payload: ProductUpdateInput) -> None:
    if payload.brand is not UNSET:
        product.brand = validate_optional_text(
            payload.brand, max_length=_MAX_BRAND_LENGTH, field_name="brand"
        )
    if payload.domain is not UNSET:
        product.domain = validate_optional_text(
            payload.domain, max_length=_MAX_DOMAIN_LENGTH, field_name="domain"
        )
    if payload.volume_ml is not UNSET:
        product.volume_ml = validate_optional_text(
            payload.volume_ml, max_length=_MAX_VOLUME_LENGTH, field_name="volume_ml"
        )
    if payload.notes is not UNSET:
        product.notes = validate_optional_text(
            payload.notes, max_length=None, field_name="notes"
        )
    if payload.rating is not UNSET:
        product.rating = validate_rating(payload.rating)


def _apply_required_updates(product: Product, payload: ProductUpdateInput) -> None:
    if payload.category is not None:
        product.category = validate_category_name(payload.category, allow_default=False)
    if payload.tags is not None:
        product.tags = json.dumps([str(tag) for tag in payload.tags])
    if payload.is_favorite is not None:
        product.is_favorite = bool(payload.is_favorite)
