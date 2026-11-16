"""Add exchange_rates cache table for historical fiat and crypto FX lookups.

Revision ID: 0004_add_exchange_rates
Revises: 0003_add_transaction_tags
Create Date: 2026-11-16 13:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004_add_exchange_rates"
down_revision: Union[str, None] = "0003_add_transaction_tags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "exchange_rates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rate_date", sa.Date(), nullable=False),
        sa.Column("from_currency", sa.String(length=10), nullable=False),
        sa.Column("to_currency", sa.String(length=10), nullable=False, server_default="NZD"),
        sa.Column("rate", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rate_date", "from_currency", "to_currency", name="uq_exchange_rate_date_pair"),
    )
    op.create_index("ix_exchange_rates_id", "exchange_rates", ["id"], unique=False)
    op.create_index("ix_exchange_rates_rate_date", "exchange_rates", ["rate_date"], unique=False)
    op.create_index("ix_exchange_rates_from_currency", "exchange_rates", ["from_currency"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_exchange_rates_from_currency", table_name="exchange_rates")
    op.drop_index("ix_exchange_rates_rate_date", table_name="exchange_rates")
    op.drop_index("ix_exchange_rates_id", table_name="exchange_rates")
    op.drop_table("exchange_rates")
