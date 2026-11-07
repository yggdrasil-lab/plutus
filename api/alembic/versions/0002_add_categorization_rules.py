"""Add categorization_rules table for Outlook-style user rules.

Revision ID: 0002_add_categorization_rules
Revises: 0001_initial_plutus_schema
Create Date: 2026-11-07 19:38:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002_add_categorization_rules"
down_revision: Union[str, None] = "0001_initial_plutus_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "categorization_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description_pattern", sa.String(length=200), nullable=False),
        sa.Column("match_type", sa.String(length=20), server_default="contains", nullable=False),
        sa.Column("amount_equals", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("target_category", sa.String(length=50), nullable=False),
        sa.Column("rename_merchant", sa.String(length=150), nullable=True),
        sa.Column("priority", sa.Integer(), server_default="10", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("match_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_categorization_rules_id", "categorization_rules", ["id"], unique=False)
    op.create_index("ix_categorization_rules_description_pattern", "categorization_rules", ["description_pattern"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_categorization_rules_description_pattern", table_name="categorization_rules")
    op.drop_index("ix_categorization_rules_id", table_name="categorization_rules")
    op.drop_table("categorization_rules")
