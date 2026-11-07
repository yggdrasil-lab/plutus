"""Add transfer reconciliation, linked transaction foreign key, exchange rate, and fee columns.

Revision ID: 0002_add_transfer_reconciliation
Revises: 0001_initial_plutus_schema
Create Date: 2026-11-07 23:59:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002_add_transfer_reconciliation"
down_revision: Union[str, None] = "0001_initial_plutus_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "transactions",
        "amount",
        type_=sa.Numeric(precision=18, scale=8),
        existing_type=sa.Numeric(precision=12, scale=2),
        nullable=False,
    )
    op.add_column("transactions", sa.Column("linked_transaction_id", sa.Integer(), nullable=True))
    op.add_column("transactions", sa.Column("exchange_rate", sa.Numeric(precision=18, scale=8), nullable=True))
    op.add_column("transactions", sa.Column("fee_amount", sa.Numeric(precision=12, scale=2), nullable=True))
    op.create_index("ix_transactions_linked_transaction_id", "transactions", ["linked_transaction_id"], unique=False)
    op.create_foreign_key(
        "fk_transactions_linked_transaction_id",
        "transactions",
        "transactions",
        ["linked_transaction_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.alter_column(
        "transactions",
        "amount",
        type_=sa.Numeric(precision=12, scale=2),
        existing_type=sa.Numeric(precision=18, scale=8),
        nullable=False,
    )
    op.drop_constraint("fk_transactions_linked_transaction_id", "transactions", type_="foreignkey")
    op.drop_index("ix_transactions_linked_transaction_id", table_name="transactions")
    op.drop_column("transactions", "fee_amount")
    op.drop_column("transactions", "exchange_rate")
    op.drop_column("transactions", "linked_transaction_id")
