"""Add recurrence to transactions and target_recurrence to categorization_rules.

Revision ID: 0007_add_recurrence_fields
Revises: 0006_add_rule_target_tags
Create Date: 2026-11-16 23:24:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007_add_recurrence_fields"
down_revision: Union[str, None] = "0006_add_rule_target_tags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column("recurrence", sa.String(length=20), nullable=True),
    )
    op.create_index("ix_transactions_recurrence", "transactions", ["recurrence"])
    op.add_column(
        "categorization_rules",
        sa.Column("target_recurrence", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("categorization_rules", "target_recurrence")
    op.drop_index("ix_transactions_recurrence", table_name="transactions")
    op.drop_column("transactions", "recurrence")
