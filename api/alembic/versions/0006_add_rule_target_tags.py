"""Add target_tags JSON column to categorization_rules table.

Revision ID: 0006_add_rule_target_tags
Revises: 0005_normalize_legacy_categories
Create Date: 2026-11-16 21:28:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006_add_rule_target_tags"
down_revision: Union[str, None] = "0005_normalize_legacy_categories"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "categorization_rules",
        sa.Column("target_tags", sa.Text(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("categorization_rules", "target_tags")
