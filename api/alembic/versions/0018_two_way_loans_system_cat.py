"""Add two-way loan_type, transaction loan_payment_amount, and Debt & Loans system flag.

Revision ID: 0018_two_way_loans_system_cat
Revises: 0017_transaction_loan_link
Create Date: 2026-11-20 15:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0018_two_way_loans_system_cat"
down_revision: Union[str, None] = "0017_transaction_loan_link"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_categories_table = sa.table(
    "categories",
    sa.column("name", sa.String),
    sa.column("is_system", sa.Boolean),
)


def upgrade() -> None:
    with op.batch_alter_table("loans") as batch_op:
        batch_op.add_column(
            sa.Column("loan_type", sa.String(length=20), nullable=False, server_default="borrowed")
        )

    with op.batch_alter_table("transactions") as batch_op:
        batch_op.add_column(
            sa.Column("loan_payment_amount", sa.Numeric(14, 2), nullable=True)
        )

    op.execute(
        _categories_table.update()
        .where(sa.func.lower(_categories_table.c.name) == "debt & loans")
        .values(is_system=True)
    )


def downgrade() -> None:
    op.execute(
        _categories_table.update()
        .where(sa.func.lower(_categories_table.c.name) == "debt & loans")
        .values(is_system=False)
    )

    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_column("loan_payment_amount")

    with op.batch_alter_table("loans") as batch_op:
        batch_op.drop_column("loan_type")
