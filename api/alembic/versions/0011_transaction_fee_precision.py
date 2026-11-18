"""Widen transactions.fee_amount precision for 8-decimal crypto miner fees.

Revision ID: 0011_transaction_fee_precision
Revises: 0010_add_budget_rule_period
Create Date: 2026-11-19 00:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0011_transaction_fee_precision"
down_revision: Union[str, None] = "0010_add_budget_rule_period"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.alter_column(
            "fee_amount",
            existing_type=sa.Numeric(12, 2),
            type_=sa.Numeric(18, 8),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.alter_column(
            "fee_amount",
            existing_type=sa.Numeric(18, 8),
            type_=sa.Numeric(12, 2),
            existing_nullable=True,
        )
