"""Add counterpart_account_id to categorization_rules and transactions.

Revision ID: 0016_rule_counterpart_account
Revises: 0015_rule_amount_bounds
Create Date: 2026-11-20 13:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0016_rule_counterpart_account"
down_revision: Union[str, None] = "0015_rule_amount_bounds"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("categorization_rules") as batch_op:
        batch_op.add_column(sa.Column("counterpart_account_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_categorization_rules_counterpart_account_id",
            "accounts",
            ["counterpart_account_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_categorization_rules_counterpart_account_id",
            ["counterpart_account_id"],
            unique=False,
        )

    with op.batch_alter_table("transactions") as batch_op:
        batch_op.add_column(sa.Column("counterpart_account_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_transactions_counterpart_account_id",
            "accounts",
            ["counterpart_account_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_transactions_counterpart_account_id",
            ["counterpart_account_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_index("ix_transactions_counterpart_account_id")
        batch_op.drop_constraint("fk_transactions_counterpart_account_id", type_="foreignkey")
        batch_op.drop_column("counterpart_account_id")

    with op.batch_alter_table("categorization_rules") as batch_op:
        batch_op.drop_index("ix_categorization_rules_counterpart_account_id")
        batch_op.drop_constraint("fk_categorization_rules_counterpart_account_id", type_="foreignkey")
        batch_op.drop_column("counterpart_account_id")
