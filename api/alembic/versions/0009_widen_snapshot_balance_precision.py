"""Widen asset_snapshots.balance precision for 8-decimal crypto balances.

Revision ID: 0009_widen_snapshot_balance_precision
Revises: 0008_add_loans_table
Create Date: 2026-11-18 13:12:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0009_widen_snapshot_balance_precision"
down_revision: Union[str, None] = "0008_add_loans_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("asset_snapshots") as batch_op:
        batch_op.alter_column(
            "balance",
            existing_type=sa.Numeric(14, 2),
            type_=sa.Numeric(18, 8),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("asset_snapshots") as batch_op:
        batch_op.alter_column(
            "balance",
            existing_type=sa.Numeric(18, 8),
            type_=sa.Numeric(14, 2),
            existing_nullable=False,
        )
