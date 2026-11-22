"""Add document_type column and index to receipts for payslip isolation.

Revision ID: 0021_receipt_document_type
Revises: 0020_email_ingestion
Create Date: 2026-11-22 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0021_receipt_document_type"
down_revision: Union[str, None] = "0020_email_ingestion"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("receipts") as batch_op:
        batch_op.add_column(
            sa.Column(
                "document_type",
                sa.String(length=32),
                nullable=False,
                server_default="receipt",
            )
        )
        batch_op.create_index("ix_receipts_document_type", ["document_type"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("receipts") as batch_op:
        batch_op.drop_index("ix_receipts_document_type")
        batch_op.drop_column("document_type")
