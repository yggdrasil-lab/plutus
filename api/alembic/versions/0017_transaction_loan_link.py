"""Add loan_id foreign key to transactions and categorization_rules.

Revision ID: 0017_transaction_loan_link
Revises: 0016_rule_counterpart_account
Create Date: 2026-11-20 14:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0017_transaction_loan_link"
down_revision: Union[str, None] = "0016_rule_counterpart_account"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.add_column(sa.Column("loan_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_transactions_loan_id",
            "loans",
            ["loan_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_transactions_loan_id",
            ["loan_id"],
            unique=False,
        )

    with op.batch_alter_table("categorization_rules") as batch_op:
        batch_op.add_column(sa.Column("loan_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_categorization_rules_loan_id",
            "loans",
            ["loan_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_categorization_rules_loan_id",
            ["loan_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("categorization_rules") as batch_op:
        batch_op.drop_index("ix_categorization_rules_loan_id")
        batch_op.drop_constraint("fk_categorization_rules_loan_id", type_="foreignkey")
        batch_op.drop_column("loan_id")

    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_index("ix_transactions_loan_id")
        batch_op.drop_constraint("fk_transactions_loan_id", type_="foreignkey")
        batch_op.drop_column("loan_id")
