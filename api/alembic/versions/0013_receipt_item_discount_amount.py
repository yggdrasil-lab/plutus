"""Add discount_amount column to receipt_items.

Revision ID: 0013_receipt_item_discount_amount
Revises: 0012_receipt_image_storage
Create Date: 2026-11-19 22:22:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0013_receipt_item_discount_amount"
down_revision: Union[str, None] = "0012_receipt_image_storage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("receipt_items") as batch_op:
        batch_op.add_column(sa.Column("discount_amount", sa.Numeric(12, 2), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("receipt_items") as batch_op:
        batch_op.drop_column("discount_amount")
