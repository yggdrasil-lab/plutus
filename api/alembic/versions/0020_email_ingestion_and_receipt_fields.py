"""Add email ingestion audit table, account suffix, receipt order/email fields, and DBS card type fix.

Revision ID: 0020_email_ingestion_and_receipt_fields
Revises: 0019_transaction_splits
Create Date: 2026-11-21 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0020_email_ingestion_and_receipt_fields"
down_revision: Union[str, None] = "0019_transaction_splits"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_accounts_table = sa.table(
    "accounts",
    sa.column("institution", sa.String),
    sa.column("name", sa.String),
    sa.column("account_type", sa.String),
)


def upgrade() -> None:
    op.create_table(
        "ingested_emails",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("message_id", sa.String(length=512), nullable=False),
        sa.Column("iris_email_id", sa.Integer(), nullable=True),
        sa.Column("route_target", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("consumer_ref", sa.String(length=256), nullable=True),
        sa.Column("transaction_id", sa.Integer(), nullable=True),
        sa.Column("receipt_ids_json", sa.Text(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name="fk_ingested_emails_transaction_id",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("message_id", name="uq_ingested_emails_message_id"),
    )
    op.create_index("ix_ingested_emails_id", "ingested_emails", ["id"], unique=False)
    op.create_index(
        "ix_ingested_emails_message_id", "ingested_emails", ["message_id"], unique=True
    )
    op.create_index(
        "ix_ingested_emails_transaction_id",
        "ingested_emails",
        ["transaction_id"],
        unique=False,
    )

    with op.batch_alter_table("accounts") as batch_op:
        batch_op.add_column(
            sa.Column("account_number_last4", sa.String(length=64), nullable=True)
        )

    with op.batch_alter_table("receipts") as batch_op:
        batch_op.add_column(
            sa.Column("order_number", sa.String(length=128), nullable=True)
        )
        batch_op.add_column(
            sa.Column("source_email_message_id", sa.String(length=512), nullable=True)
        )
        batch_op.create_index(
            "ix_receipts_order_number", ["order_number"], unique=False
        )
        batch_op.create_index(
            "ix_receipts_source_email_message_id",
            ["source_email_message_id"],
            unique=False,
        )

    op.execute(
        _accounts_table.update()
        .where(sa.func.lower(_accounts_table.c.institution).like("%dbs%"))
        .where(sa.func.lower(_accounts_table.c.name).like("%live fresh%"))
        .where(_accounts_table.c.account_type != "credit_card")
        .values(account_type="credit_card")
    )


def downgrade() -> None:
    with op.batch_alter_table("receipts") as batch_op:
        batch_op.drop_index("ix_receipts_source_email_message_id")
        batch_op.drop_index("ix_receipts_order_number")
        batch_op.drop_column("source_email_message_id")
        batch_op.drop_column("order_number")

    with op.batch_alter_table("accounts") as batch_op:
        batch_op.drop_column("account_number_last4")

    op.drop_index("ix_ingested_emails_transaction_id", table_name="ingested_emails")
    op.drop_index("ix_ingested_emails_message_id", table_name="ingested_emails")
    op.drop_index("ix_ingested_emails_id", table_name="ingested_emails")
    op.drop_table("ingested_emails")
