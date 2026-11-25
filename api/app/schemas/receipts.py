"""Receipt and itemized product telemetry schemas."""

from datetime import date, datetime
import json
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class ReceiptItemBase(BaseModel):
    item_name: str = Field(..., max_length=200)
    product_id: Optional[int] = None
    brand: Optional[str] = Field(default=None, max_length=100)
    quantity: float = Field(default=1.0, ge=0.01)
    unit_price: float = Field(...)
    discount_amount: Optional[float] = Field(default=None, ge=0.0)
    total_price: float = Field(...)
    category: str = Field(default="General", max_length=50)
    domain: Optional[str] = Field(default=None, max_length=50)
    product_type: Optional[str] = Field(default=None, max_length=50)
    tags: list[str] = Field(default_factory=list)
    volume_ml: Optional[str] = Field(default=None, max_length=50)
    notes: Optional[str] = None

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


class ReceiptItemCreate(ReceiptItemBase):
    pass


class ReceiptItemUpdate(BaseModel):
    category: Optional[str] = Field(default=None, max_length=50)
    domain: Optional[str] = Field(default=None, max_length=50)
    tags: Optional[list[str]] = None
    notes: Optional[str] = None


class ReceiptItemRead(ReceiptItemBase):
    id: int
    receipt_id: int
    inherited_tags: list[str] = Field(default_factory=list)
    created_at: datetime

    class Config:
        from_attributes = True


VALID_RECEIPT_DOCUMENT_TYPES = {"receipt", "payslip"}


def normalize_receipt_document_type(value: Any) -> str:
    """Normalize receipt document_type to 'receipt' or 'payslip'."""
    if value is None:
        return "receipt"
    cleaned = str(value).strip().lower()
    if cleaned not in VALID_RECEIPT_DOCUMENT_TYPES:
        raise ValueError(
            f"Unsupported document_type '{value}'. Must be one of: {', '.join(sorted(VALID_RECEIPT_DOCUMENT_TYPES))}."
        )
    return cleaned


class ReceiptBase(BaseModel):
    document_type: str = Field(default="receipt", max_length=32)
    receipt_date: date
    merchant: str = Field(..., max_length=150)
    total_amount: float = Field(..., ge=0.0)
    currency: str = Field(default="NZD", max_length=3)
    tax_amount: Optional[float] = Field(default=None, ge=0.0)
    order_number: Optional[str] = Field(default=None, max_length=128)
    status: str = Field(default="unlinked", max_length=30)
    notes: Optional[str] = None

    @field_validator("document_type", mode="before")
    @classmethod
    def validate_document_type(cls, v: Any) -> str:
        return normalize_receipt_document_type(v)


class ReceiptCreate(ReceiptBase):
    items: list[ReceiptItemCreate] = []


class ReceiptRead(ReceiptBase):
    id: int
    transaction_id: Optional[int] = None
    source_email_message_id: Optional[str] = None
    image_path: Optional[str] = None
    image_mime_type: Optional[str] = None
    image_filename: Optional[str] = None
    has_image: bool = False
    is_processed: bool = False
    raw_text: Optional[str] = None
    items: list[ReceiptItemRead] = []
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TransactionCandidateRead(BaseModel):
    transaction_id: int
    transaction_date: date
    merchant: str
    raw_description: Optional[str] = None
    amount: float
    currency: str
    account_id: int
    account_name: Optional[str] = None
    original_amount: Optional[float] = None
    original_currency: Optional[str] = None
    estimated_receipt_amount: Optional[float] = None
    confidence: float
    match_reason: str


class ProductStatisticItem(BaseModel):
    item_name: str
    brand: Optional[str] = None
    category: str
    domain: Optional[str] = None
    product_type: Optional[str] = None
    volume_ml: Optional[str] = None
    purchase_count: int
    total_spent: float
    unit_price_latest: float
    first_purchased: date
    last_purchased: date
    avg_days_between_purchases: Optional[float] = None
    estimated_depletion_date: Optional[date] = None


class ProductStatisticsResponse(BaseModel):
    domain: Optional[str] = None
    total_products: int
    total_spend: float
    products: list[ProductStatisticItem]
