"""Initial Plutus schema for accounts, snapshots, transactions, and runway.

Revision ID: 0001_initial_plutus_schema
Revises: 
Create Date: 2026-11-04 15:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001_initial_plutus_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Accounts
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("account_type", sa.String(length=30), nullable=False),
        sa.Column("tier", sa.String(length=20), server_default="cash", nullable=False),
        sa.Column("institution", sa.String(length=100), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="NZD", nullable=False),
        sa.Column("expected_annual_return", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_accounts_id", "accounts", ["id"], unique=False)

    # 2. Asset Snapshots
    op.create_table(
        "asset_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("balance", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="NZD", nullable=False),
        sa.Column("exchange_rate", sa.Numeric(precision=10, scale=4), server_default="1.0", nullable=False),
        sa.Column("balance_nzd", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("source", sa.String(length=30), server_default="manual", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("snapshot_date", "account_id", name="uq_snapshot_date_account"),
    )
    op.create_index("ix_asset_snapshots_id", "asset_snapshots", ["id"], unique=False)
    op.create_index("ix_asset_snapshots_snapshot_date", "asset_snapshots", ["snapshot_date"], unique=False)

    # 3. Transactions
    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="NZD", nullable=False),
        sa.Column("merchant", sa.String(length=150), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("raw_description", sa.Text(), nullable=True),
        sa.Column("raw_hash", sa.String(length=64), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transactions_id", "transactions", ["id"], unique=False)
    op.create_index("ix_transactions_transaction_date", "transactions", ["transaction_date"], unique=False)
    op.create_index("ix_transactions_category", "transactions", ["category"], unique=False)
    op.create_index("ix_transactions_raw_hash", "transactions", ["raw_hash"], unique=True)

    # 4. Budget Rules
    op.create_table(
        "budget_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("weekly_limit", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("monthly_limit", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("category"),
    )
    op.create_index("ix_budget_rules_id", "budget_rules", ["id"], unique=False)

    # 5. Runway Snapshots
    op.create_table(
        "runway_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("weekly_burn_rate", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("cash_assets", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("growth_assets", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("locked_assets", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("emergency_runway_weeks", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("growth_runway_weeks", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("is_perpetual", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("assumed_real_growth_rate", sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("snapshot_date"),
    )
    op.create_index("ix_runway_snapshots_id", "runway_snapshots", ["id"], unique=False)
    op.create_index("ix_runway_snapshots_snapshot_date", "runway_snapshots", ["snapshot_date"], unique=False)


def downgrade() -> None:
    op.drop_table("runway_snapshots")
    op.drop_table("budget_rules")
    op.drop_table("transactions")
    op.drop_table("asset_snapshots")
    op.drop_table("accounts")
