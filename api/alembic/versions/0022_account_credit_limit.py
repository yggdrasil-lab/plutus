"""Add credit_limit column to accounts for revolving credit backstop telemetry.

Revision ID: 0022_account_credit_limit
Revises: 0021_receipt_document_type
Create Date: 2026-11-27 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0022_account_credit_limit"
down_revision: Union[str, None] = "0021_receipt_document_type"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.add_column(sa.Column("credit_limit", sa.Numeric(18, 2), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.drop_column("credit_limit")
