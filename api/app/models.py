"""SQLAlchemy database models for Plutus wealth and runway system."""

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utc_now() -> datetime:
    """Return timezone-aware current UTC datetime."""
    return datetime.now(timezone.utc)


class Account(Base):
    """Financial account or asset bucket."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    account_type: Mapped[str] = mapped_column(String(30), nullable=False)  # checking, savings, brokerage, kiwisaver, crypto
    tier: Mapped[str] = mapped_column(String(20), nullable=False, default="cash")  # cash, growth, locked
    institution: Mapped[str] = mapped_column(String(100), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="NZD")
    expected_annual_return: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    snapshots: Mapped[list["AssetSnapshot"]] = relationship("AssetSnapshot", back_populates="account", cascade="all, delete-orphan")
    transactions: Mapped[list["Transaction"]] = relationship("Transaction", back_populates="account")


class AssetSnapshot(Base):
    """Point-in-time valuation of an account balance."""

    __tablename__ = "asset_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_date", "account_id", name="uq_snapshot_date_account"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    balance: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="NZD")
    exchange_rate: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False, default=1.0)
    balance_nzd: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="manual")  # manual, statement_pdf, csv, mcp
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    account: Mapped["Account"] = relationship("Account", back_populates="snapshots")


class Transaction(Base):
    """Individual financial transaction or expense."""

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id"), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="NZD")
    merchant: Mapped[str] = mapped_column(String(150), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    raw_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    account: Mapped["Account"] = relationship("Account", back_populates="transactions")
    receipt: Mapped[Optional["Receipt"]] = relationship(
        "Receipt",
        back_populates="transaction",
        uselist=False,
        primaryjoin="Transaction.id == Receipt.transaction_id",
    )


class BudgetRule(Base):
    """Spending targets per category."""

    __tablename__ = "budget_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    category: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    weekly_limit: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    monthly_limit: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class RunwaySnapshot(Base):
    """Historical record of computed financial runway metrics."""

    __tablename__ = "runway_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    snapshot_date: Mapped[date] = mapped_column(Date, unique=True, nullable=False, index=True)
    weekly_burn_rate: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    cash_assets: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    growth_assets: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    locked_assets: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    emergency_runway_weeks: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    growth_runway_weeks: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    is_perpetual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    assumed_real_growth_rate: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CategorizationRule(Base):
    """Outlook-style transaction categorization and routing rule."""

    __tablename__ = "categorization_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description_pattern: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    match_type: Mapped[str] = mapped_column(String(20), nullable=False, default="contains")
    amount_equals: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    target_category: Mapped[str] = mapped_column(String(50), nullable=False)
    rename_merchant: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    match_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class Product(Base):
    """Canonical inventory catalog item for cross-domain tracking, notes, and price history."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    brand: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    category: Mapped[str] = mapped_column(String(50), nullable=False, default="General", index=True)
    domain: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, index=True)  # e.g. "02-skin", "01-health", "20-home"
    tags: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON-encoded list of tags e.g. ["skincare", "serum"]
    volume_ml: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # User notes/observations about the product
    rating: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 1-5 scale
    is_favorite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    receipt_items: Mapped[list["ReceiptItem"]] = relationship("ReceiptItem", back_populates="product")


class Receipt(Base):
    """Itemized physical or electronic receipt."""

    __tablename__ = "receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    transaction_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("transactions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    receipt_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    merchant: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    total_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="NZD")
    tax_amount: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="unlinked")  # unlinked, matched, verified
    image_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    raw_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    transaction: Mapped[Optional["Transaction"]] = relationship("Transaction", back_populates="receipt", foreign_keys=[transaction_id])
    items: Mapped[list["ReceiptItem"]] = relationship("ReceiptItem", back_populates="receipt", cascade="all, delete-orphan")


class ReceiptItem(Base):
    """Granular line item from a receipt for cross-domain product tracking."""

    __tablename__ = "receipt_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    receipt_id: Mapped[int] = mapped_column(
        ForeignKey("receipts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    item_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    brand: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    total_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False, default="General", index=True)
    domain: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, index=True)  # e.g. "02-skin", "01-health", "20-home", "general"
    product_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, index=True)  # e.g. "serum", "cleanser", "protein"
    tags: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON-encoded tags e.g. ["skincare", "serum"]
    volume_ml: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # e.g. "30ml", "1kg", "60 caps"
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    receipt: Mapped["Receipt"] = relationship("Receipt", back_populates="items")
    product: Mapped[Optional["Product"]] = relationship("Product", back_populates="receipt_items")
