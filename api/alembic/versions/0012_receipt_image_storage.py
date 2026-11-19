"""Add database image storage and processing status columns to receipts.

Revision ID: 0012_receipt_image_storage
Revises: 0011_transaction_fee_precision
Create Date: 2026-11-19 18:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0012_receipt_image_storage"
down_revision: Union[str, None] = "0011_transaction_fee_precision"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("receipts") as batch_op:
        batch_op.add_column(sa.Column("image_data", sa.LargeBinary(), nullable=True))
        batch_op.add_column(sa.Column("image_mime_type", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("image_filename", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column(
                "is_processed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("receipts") as batch_op:
        batch_op.drop_column("is_processed")
        batch_op.drop_column("image_filename")
        batch_op.drop_column("image_mime_type")
        batch_op.drop_column("image_data")
