"""Add external provider sync metadata columns to accounts.

Revision ID: 0002_add_external_sync_fields
Revises: 0001_initial_plutus_schema
Create Date: 2026-11-16 11:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002_add_external_sync_fields"
down_revision: Union[str, None] = "0001_initial_plutus_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("external_provider", sa.String(length=30), nullable=True))
    op.add_column("accounts", sa.Column("external_account_id", sa.String(length=150), nullable=True))
    op.add_column("accounts", sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_accounts_external_provider", "accounts", ["external_provider"], unique=False)
    op.create_index("ix_accounts_external_account_id", "accounts", ["external_account_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_accounts_external_account_id", table_name="accounts")
    op.drop_index("ix_accounts_external_provider", table_name="accounts")
    op.drop_column("accounts", "last_synced_at")
    op.drop_column("accounts", "external_account_id")
    op.drop_column("accounts", "external_provider")
