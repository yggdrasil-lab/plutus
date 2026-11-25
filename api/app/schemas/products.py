"""Product catalog and price history schemas."""

from datetime import date, datetime
import json
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class PricePoint(BaseModel):
    """Historical unit price record from an itemized receipt."""

    date: date
    merchant: str
    unit_price: float
    discount_amount: Optional[float] = None
    total_price: float
    quantity: float
    receipt_id: int
    receipt_item_id: int


class ProductBase(BaseModel):
    """Core inventory/catalog product attributes."""

    name: str = Field(..., max_length=200)
    brand: Optional[str] = Field(default=None, max_length=100)
    category: str = Field(default="General", max_length=50)
    domain: Optional[str] = Field(default=None, max_length=50)
    tags: list[str] = Field(default_factory=list)
    volume_ml: Optional[str] = Field(default=None, max_length=50)
    notes: Optional[str] = None
    rating: Optional[int] = Field(default=None, ge=1, le=5)
    is_favorite: bool = False

    @field_validator("tags", mode="before")
    @classmethod
    def parse_tags_if_string(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [str(t) for t in parsed]
            except Exception:
                return [t.strip() for t in v.split(",") if t.strip()]
        if isinstance(v, list):
            return [str(t) for t in v]
        return []


class ProductCreate(ProductBase):
    pass


class ProductUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    brand: Optional[str] = Field(default=None, max_length=100)
    category: Optional[str] = Field(default=None, max_length=50)
    domain: Optional[str] = Field(default=None, max_length=50)
    tags: Optional[list[str]] = None
    volume_ml: Optional[str] = Field(default=None, max_length=50)
    notes: Optional[str] = None
    rating: Optional[int] = Field(default=None, ge=1, le=5)
    is_favorite: Optional[bool] = None


class ProductRead(ProductBase):
    id: int
    purchase_count: int = 0
    total_spent: float = 0.0
    latest_price: Optional[float] = None
    lowest_price: Optional[float] = None
    highest_price: Optional[float] = None
    average_price: Optional[float] = None
    price_change_latest: Optional[float] = None  # Price difference compared to prior purchase
    first_purchased: Optional[date] = None
    last_purchased: Optional[date] = None
    avg_days_between_purchases: Optional[float] = None
    estimated_depletion_date: Optional[date] = None
    price_history: list[PricePoint] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ProductListResponse(BaseModel):
    total_count: int
    all_tags: list[str] = Field(default_factory=list)
    products: list[ProductRead] = Field(default_factory=list)
