"""SQLAlchemy database models for Plutus wealth and runway system."""

from datetime import date, datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
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
    account_type: Mapped[str] = mapped_column(String(30), nullable=False)  # checking, savings, brokerage, kiwisaver, crypto, credit_card, private_note
    tier: Mapped[str] = mapped_column(String(20), nullable=False, default="cash")  # cash, growth, locked
    institution: Mapped[str] = mapped_column(String(100), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="NZD")
    expected_annual_return: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    external_provider: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, index=True)
    external_account_id: Mapped[Optional[str]] = mapped_column(String(150), nullable=True, index=True)
    account_number_last4: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    snapshots: Mapped[list["AssetSnapshot"]] = relationship("AssetSnapshot", back_populates="account", cascade="all, delete-orphan")
    transactions: Mapped[list["Transaction"]] = relationship(
        "Transaction",
        back_populates="account",
        foreign_keys="Transaction.account_id",
    )


class AssetSnapshot(Base):
    """Point-in-time valuation of an account balance."""

    __tablename__ = "asset_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_date", "account_id", name="uq_snapshot_date_account"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    balance: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
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
    amount: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="NZD")
    merchant: Mapped[str] = mapped_column(String(150), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    raw_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    linked_transaction_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True, index=True)
    counterpart_account_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    loan_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("loans.id", ondelete="SET NULL"), nullable=True, index=True)
    loan_payment_amount: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    exchange_rate: Mapped[Optional[float]] = mapped_column(Numeric(18, 8), nullable=True)
    fee_amount: Mapped[Optional[float]] = mapped_column(Numeric(18, 8), nullable=True)
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    recurrence: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    account: Mapped["Account"] = relationship(
        "Account",
        back_populates="transactions",
        foreign_keys=[account_id],
    )
    counterpart_account: Mapped[Optional["Account"]] = relationship(
        "Account",
        foreign_keys=[counterpart_account_id],
    )
    loan: Mapped[Optional["Loan"]] = relationship(
        "Loan",
        back_populates="transactions",
        foreign_keys=[loan_id],
    )
    linked_transaction: Mapped[Optional["Transaction"]] = relationship(
        "Transaction",
        remote_side="Transaction.id",
        foreign_keys=[linked_transaction_id],
        uselist=False,
    )
    receipt: Mapped[Optional["Receipt"]] = relationship(
        "Receipt",
        back_populates="transaction",
        uselist=False,
        primaryjoin="Transaction.id == Receipt.transaction_id",
    )
    splits: Mapped[list["TransactionSplit"]] = relationship(
        "TransactionSplit",
        back_populates="transaction",
        cascade="all, delete-orphan",
        order_by="TransactionSplit.id",
        foreign_keys="TransactionSplit.transaction_id",
    )


class TransactionSplit(Base):
    """Manual split line allocation belonging to a parent transaction."""

    __tablename__ = "transaction_splits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    transaction_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    loan_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("loans.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    notes: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    transaction: Mapped["Transaction"] = relationship(
        "Transaction",
        back_populates="splits",
        foreign_keys=[transaction_id],
    )
    loan: Mapped[Optional["Loan"]] = relationship(
        "Loan",
        back_populates="split_entries",
        foreign_keys=[loan_id],
    )

    def signed_amount(
        self,
        *,
        parent_amount: Optional[float] = None,
        siblings: Optional[Sequence["TransactionSplit"]] = None,
    ) -> float:
        """Return this split line's signed amount, normalizing legacy unsigned positive splits on negative parents."""
        raw = float(self.amount)
        tx = getattr(self, "transaction", None)
        resolved_parent = (
            float(parent_amount)
            if parent_amount is not None
            else (float(tx.amount) if tx is not None and tx.amount is not None else 0.0)
        )
        resolved_siblings = (
            siblings
            if siblings is not None
            else (getattr(tx, "splits", None) or [self])
        )
        if resolved_parent < 0 and resolved_siblings and all(float(s.amount) > 0 for s in resolved_siblings):
            return -raw
        return raw


class Category(Base):
    """Hierarchical spending category with optional 52-week annual amortization."""

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    parent_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    amortize_period: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default="100"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    parent: Mapped[Optional["Category"]] = relationship(
        "Category",
        remote_side="Category.id",
        back_populates="children",
    )
    children: Mapped[list["Category"]] = relationship(
        "Category",
        back_populates="parent",
    )


class BudgetRule(Base):
    """Spending targets per category."""

    __tablename__ = "budget_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    category: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    weekly_limit: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    monthly_limit: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    budget_period: Mapped[str] = mapped_column(
        String(20), nullable=False, default="monthly", server_default="monthly"
    )
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
    min_amount: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    max_amount: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    direction: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    target_category: Mapped[str] = mapped_column(String(50), nullable=False)
    target_tags: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    target_recurrence: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    rename_merchant: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    counterpart_account_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    loan_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("loans.id", ondelete="SET NULL"), nullable=True, index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    match_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    counterpart_account: Mapped[Optional["Account"]] = relationship(
        "Account",
        foreign_keys=[counterpart_account_id],
    )
    loan: Mapped[Optional["Loan"]] = relationship(
        "Loan",
        foreign_keys=[loan_id],
    )


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
    document_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="receipt",
        server_default="receipt",
        index=True,
    )
    receipt_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    merchant: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    total_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="NZD")
    tax_amount: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    order_number: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    source_email_message_id: Mapped[Optional[str]] = mapped_column(String(512), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="unlinked")  # unlinked, needs_review, matched, verified
    image_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    image_data: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    image_mime_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    image_filename: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_processed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    raw_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    transaction: Mapped[Optional["Transaction"]] = relationship("Transaction", back_populates="receipt", foreign_keys=[transaction_id])
    items: Mapped[list["ReceiptItem"]] = relationship("ReceiptItem", back_populates="receipt", cascade="all, delete-orphan")

    @property
    def has_image(self) -> bool:
        """Return True when binary receipt media is stored in the database."""
        return self.image_data is not None and len(self.image_data) > 0


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
    discount_amount: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
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

    @property
    def inherited_tags(self) -> list[str]:
        """Return normalized tags inherited from the linked parent transaction."""
        if not self.receipt or not self.receipt.transaction:
            return []
        from app.services.ledger_entry.tags import parse_rule_tags

        return parse_rule_tags(self.receipt.transaction.tags)


class ExchangeRate(Base):
    """Cached historical and spot FX rates for fiat and crypto conversions."""

    __tablename__ = "exchange_rates"
    __table_args__ = (
        UniqueConstraint("rate_date", "from_currency", "to_currency", name="uq_exchange_rate_date_pair"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    rate_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    from_currency: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    to_currency: Mapped[str] = mapped_column(String(10), nullable=False, default="NZD")
    rate: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Loan(Base):
    """Financed liability or lent receivable tracked with transaction-driven amortization."""

    __tablename__ = "loans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    lender: Mapped[str] = mapped_column(String(120), nullable=False)
    loan_type: Mapped[str] = mapped_column(String(20), default="borrowed", nullable=False)
    principal_amount: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    annual_interest_rate: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)
    term_months: Mapped[int] = mapped_column(Integer, nullable=False)
    monthly_payment: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    asset_value: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="NZD")
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    transactions: Mapped[list["Transaction"]] = relationship(
        "Transaction",
        back_populates="loan",
        foreign_keys="Transaction.loan_id",
    )
    split_entries: Mapped[list["TransactionSplit"]] = relationship(
        "TransactionSplit",
        back_populates="loan",
        foreign_keys="TransactionSplit.loan_id",
    )


class IngestedEmail(Base):
    """Audit and idempotency record for inbound emails routed from Iris."""

    __tablename__ = "ingested_emails"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    message_id: Mapped[str] = mapped_column(String(512), unique=True, nullable=False, index=True)
    iris_email_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    route_target: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    consumer_ref: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    transaction_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("transactions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    receipt_ids_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    transaction: Mapped[Optional["Transaction"]] = relationship(
        "Transaction",
        foreign_keys=[transaction_id],
    )
