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
