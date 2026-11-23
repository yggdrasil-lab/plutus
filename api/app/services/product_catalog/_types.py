"""Pure domain constants, exceptions, validation helpers, and frozen contracts for Product Catalog."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
import json
from typing import Optional

_MAX_NAME_LENGTH = 200
_MAX_BRAND_LENGTH = 100
_MAX_CATEGORY_LENGTH = 50
_MAX_DOMAIN_LENGTH = 50
_MAX_VOLUME_LENGTH = 50
_MIN_RATING = 1
_MAX_RATING = 5
_MAX_LIST_LIMIT = 500

TagResolver = Callable[[str, Optional[str]], Sequence[str]]


class ProductCatalogError(ValueError):
    """Base domain exception for product catalog operations."""


class ProductNotFoundError(ProductCatalogError):
    """Raised when a target product cannot be found."""


class ProductConflictError(ProductCatalogError):
    """Raised when a product name collides case-insensitively with an existing row."""


class ProductValidationError(ProductCatalogError):
    """Raised when product attributes or query parameters violate catalog constraints."""


class _UnsetType:
    """Sentinel distinguishing omitted update fields from explicit None values."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _UnsetType()


@dataclass(frozen=True)
class PricePointSnapshot:
    """Detached immutable historical unit price point from a receipt item."""

    date: date
    merchant: str
    unit_price: float
    total_price: float
    quantity: float
    receipt_id: int
    receipt_item_id: int
    discount_amount: Optional[float] = None


@dataclass(frozen=True)
class ProductSnapshot:
    """Detached immutable view of a catalog product and its purchase price telemetry."""

    id: int
    name: str
    created_at: datetime
    updated_at: datetime
    brand: Optional[str] = None
    category: str = "General"
    domain: Optional[str] = None
    tags: tuple[str, ...] = ()
    volume_ml: Optional[str] = None
    notes: Optional[str] = None
    rating: Optional[int] = None
    is_favorite: bool = False
    purchase_count: int = 0
    total_spent: float = 0.0
    latest_price: Optional[float] = None
    lowest_price: Optional[float] = None
    highest_price: Optional[float] = None
    average_price: Optional[float] = None
    price_change_latest: Optional[float] = None
    first_purchased: Optional[date] = None
    last_purchased: Optional[date] = None
    avg_days_between_purchases: Optional[float] = None
    estimated_depletion_date: Optional[date] = None
    price_history: tuple[PricePointSnapshot, ...] = ()


@dataclass(frozen=True)
class ProductListResult:
    """Detached paginated product list with distinct indexed catalog tags."""

    total_count: int
    all_tags: tuple[str, ...] = ()
    products: tuple[ProductSnapshot, ...] = ()


@dataclass(frozen=True)
class ProductListQuery:
    """Typed filter and pagination parameters for listing catalog products."""

    domain: Optional[str] = None
    tag: Optional[str] = None
    search: Optional[str] = None
    is_favorite: Optional[bool] = None
    limit: int = 100
    offset: int = 0


@dataclass(frozen=True)
class ProductCreateInput:
    """Typed input contract for creating a catalog product."""

    name: str
    brand: Optional[str] = None
    category: str = "General"
    domain: Optional[str] = None
    tags: Sequence[str] = ()
    volume_ml: Optional[str] = None
    notes: Optional[str] = None
    rating: Optional[int] = None
    is_favorite: bool = False


@dataclass(frozen=True)
class ProductUpdateInput:
    """Typed partial-update contract using UNSET for nullable clearable attributes."""

    name: Optional[str] = None
    brand: str | None | _UnsetType = UNSET
    category: Optional[str] = None
    domain: str | None | _UnsetType = UNSET
    tags: Optional[Sequence[str]] = None
    volume_ml: str | None | _UnsetType = UNSET
    notes: str | None | _UnsetType = UNSET
    rating: int | None | _UnsetType = UNSET
    is_favorite: Optional[bool] = None


@dataclass(frozen=True)
class ProductStatisticSnapshot:
    """Detached immutable aggregated spend and longevity statistics for one item name."""

    item_name: str
    category: str
    purchase_count: int
    total_spent: float
    unit_price_latest: float
    first_purchased: date
    last_purchased: date
    brand: Optional[str] = None
    domain: Optional[str] = None
    product_type: Optional[str] = None
    volume_ml: Optional[str] = None
    avg_days_between_purchases: Optional[float] = None
    estimated_depletion_date: Optional[date] = None


@dataclass(frozen=True)
class ProductStatisticsSnapshot:
    """Detached immutable statistics summary across filtered receipt line items."""

    domain: Optional[str] = None
    total_products: int = 0
    total_spend: float = 0.0
    products: tuple[ProductStatisticSnapshot, ...] = ()


def parse_product_tags(raw_tags: Optional[str]) -> tuple[str, ...]:
    """Decode JSON-encoded or comma-separated product tags safely."""
    if not raw_tags:
        return ()
    try:
        parsed = json.loads(raw_tags)
    except (json.JSONDecodeError, TypeError):
        return tuple(tag.strip() for tag in raw_tags.split(",") if tag.strip())
    if isinstance(parsed, list):
        return tuple(str(tag) for tag in parsed if str(tag).strip())
    return ()


def normalize_product_name(name: str) -> str:
    """Trim and validate a canonical product name."""
    cleaned = (name or "").strip()
    if not cleaned:
        raise ProductValidationError("Product name cannot be empty.")
    if len(cleaned) > _MAX_NAME_LENGTH:
        raise ProductValidationError(
            f"Product name must be {_MAX_NAME_LENGTH} characters or fewer."
        )
    return cleaned


def validate_category_name(category: Optional[str], *, allow_default: bool = False) -> str:
    """Validate and trim a product category label."""
    cleaned = (category or "").strip()
    if not cleaned:
        if allow_default:
            return "General"
        raise ProductValidationError("Product category cannot be empty.")
    if len(cleaned) > _MAX_CATEGORY_LENGTH:
        raise ProductValidationError(
            f"Product category must be {_MAX_CATEGORY_LENGTH} characters or fewer."
        )
    return cleaned


def validate_optional_text(
    value: Optional[str], *, max_length: Optional[int], field_name: str
) -> Optional[str]:
    """Trim an optional string field and enforce its column length ceiling."""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if max_length is not None and len(cleaned) > max_length:
        raise ProductValidationError(
            f"Product {field_name} must be {max_length} characters or fewer."
        )
    return cleaned


def validate_rating(rating: Optional[int]) -> Optional[int]:
    """Validate optional 1-5 rating bounds."""
    if rating is None:
        return None
    if rating < _MIN_RATING or rating > _MAX_RATING:
        raise ProductValidationError(
            f"Product rating must be between {_MIN_RATING} and {_MAX_RATING}."
        )
    return rating
