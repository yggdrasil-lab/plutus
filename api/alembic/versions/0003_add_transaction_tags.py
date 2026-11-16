"""Add tags JSON column to transactions table.

Revision ID: 0003_add_transaction_tags
Revises: 0002_add_external_sync_fields
Create Date: 2026-11-16 13:02:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003_add_transaction_tags"
down_revision: Union[str, None] = "0002_add_external_sync_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column("tags", sa.Text(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("transactions", "tags")
