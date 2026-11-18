"""Add budget_period to budget_rules for weekly, monthly, or annual (52w) budgets.

Revision ID: 0010_add_budget_rule_period
Revises: 0009_snapshot_balance_precision
Create Date: 2026-11-18 18:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0010_add_budget_rule_period"
down_revision: Union[str, None] = "0009_snapshot_balance_precision"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("budget_rules") as batch_op:
        batch_op.add_column(
            sa.Column(
                "budget_period",
                sa.String(length=20),
                nullable=False,
                server_default="monthly",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("budget_rules") as batch_op:
        batch_op.drop_column("budget_period")
