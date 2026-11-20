"""Create transaction_splits table for manual multi-category and loan splits.

Revision ID: 0019_transaction_splits
Revises: 0018_two_way_loans_system_cat
Create Date: 2026-11-20 15:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0019_transaction_splits"
down_revision: Union[str, None] = "0018_two_way_loans_system_cat"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "transaction_splits",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("tags", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("loan_id", sa.Integer(), nullable=True),
        sa.Column("notes", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name="fk_transaction_splits_transaction_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["loan_id"],
            ["loans.id"],
            name="fk_transaction_splits_loan_id",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_transaction_splits_id",
        "transaction_splits",
        ["id"],
        unique=False,
    )
    op.create_index(
        "ix_transaction_splits_transaction_id",
        "transaction_splits",
        ["transaction_id"],
        unique=False,
    )
    op.create_index(
        "ix_transaction_splits_loan_id",
        "transaction_splits",
        ["loan_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_transaction_splits_loan_id", table_name="transaction_splits")
    op.drop_index("ix_transaction_splits_transaction_id", table_name="transaction_splits")
    op.drop_index("ix_transaction_splits_id", table_name="transaction_splits")
    op.drop_table("transaction_splits")
