"""Add min_amount, max_amount, and direction to categorization_rules.

Revision ID: 0015_rule_amount_bounds
Revises: 0014_categories_table
Create Date: 2026-11-20 11:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0015_rule_amount_bounds"
down_revision: Union[str, None] = "0014_categories_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "categorization_rules",
        sa.Column("min_amount", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column(
        "categorization_rules",
        sa.Column("max_amount", sa.Numeric(12, 2), nullable=True),
    )
    op.add_column(
        "categorization_rules",
        sa.Column("direction", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("categorization_rules", "direction")
    op.drop_column("categorization_rules", "max_amount")
    op.drop_column("categorization_rules", "min_amount")
