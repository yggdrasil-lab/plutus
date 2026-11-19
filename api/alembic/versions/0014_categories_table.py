"""Create categories table and seed canonical 2-level hierarchy.

Revision ID: 0014_categories_table
Revises: 0013_receipt_item_discount
Create Date: 2026-11-20 09:30:00.000000

"""
from datetime import datetime, timezone
from typing import Optional, Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0014_categories_table"
down_revision: Union[str, None] = "0013_receipt_item_discount"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (name, parent_name, is_system, sort_order)
SEED_CATEGORIES: tuple[tuple[str, Optional[str], bool, int], ...] = (
    ("Food", None, False, 10),
    ("Dining", "Food", False, 11),
    ("Groceries", "Food", False, 12),
    ("Alcohol", "Food", False, 13),
    ("Housing", None, False, 20),
    ("Rent", "Housing", False, 21),
    ("Household", "Housing", False, 22),
    ("Transport", None, False, 30),
    ("Fuel", "Transport", False, 31),
    ("Transit", "Transport", False, 32),
    ("Vehicle", "Transport", False, 33),
    ("Health", None, False, 40),
    ("Fitness", "Health", False, 41),
    ("Medical", "Health", False, 42),
    ("Supplements", "Health", False, 43),
    ("Personal Care", "Health", False, 44),
    ("Shopping", None, False, 50),
    ("Clothing", "Shopping", False, 51),
    ("Tech", "Shopping", False, 52),
    ("Utilities", None, False, 60),
    ("Bills & Telco", "Utilities", False, 61),
    ("Software & AI", "Utilities", False, 62),
    ("Insurance", None, False, 70),
    ("Leisure", None, False, 80),
    ("Gaming", "Leisure", False, 81),
    ("Travel", None, False, 90),
    ("Family", None, False, 100),
    ("Finance", None, False, 110),
    ("Fees & Admin", "Finance", False, 111),
    ("Debt & Loans", "Finance", False, 112),
    ("General", None, True, 900),
    ("Transfers", None, True, 910),
    ("Income", None, True, 920),
)


def upgrade() -> None:
    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("amortize_period", sa.String(length=20), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("100")),
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
            ["parent_id"],
            ["categories.id"],
            name="fk_categories_parent_id",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("name", name="uq_categories_name"),
    )
    op.create_index("ix_categories_id", "categories", ["id"], unique=False)
    op.create_index("ix_categories_name", "categories", ["name"], unique=True)
    op.create_index("ix_categories_parent_id", "categories", ["parent_id"], unique=False)

    bind = op.get_bind()
    now = datetime.now(timezone.utc)

    annual_budget_cats: set[str] = set()
    budget_rows = bind.execute(
        sa.text("SELECT category, budget_period, is_active FROM budget_rules")
    ).fetchall()
    for cat_name, budget_period, is_active in budget_rows:
        if is_active and cat_name and (budget_period or "").strip().lower() == "annual":
            annual_budget_cats.add(cat_name.strip().lower())

    insert_stmt = sa.text(
        "INSERT INTO categories (name, parent_id, is_system, is_active, amortize_period, sort_order, created_at, updated_at) "
        "VALUES (:name, :parent_id, :is_system, :is_active, :amortize_period, :sort_order, :created_at, :updated_at)"
    )

    # 1. Insert root categories first
    seen_lower: set[str] = set()
    for name, parent_name, is_system, sort_order in SEED_CATEGORIES:
        if parent_name is not None:
            continue
        amort = "annual" if name.lower() in annual_budget_cats else None
        bind.execute(
            insert_stmt,
            {
                "name": name,
                "parent_id": None,
                "is_system": is_system,
                "is_active": True,
                "amortize_period": amort,
                "sort_order": sort_order,
                "created_at": now,
                "updated_at": now,
            },
        )
        seen_lower.add(name.lower())

    # 2. Resolve inserted root IDs and insert child categories
    root_rows = bind.execute(sa.text("SELECT id, name FROM categories")).fetchall()
    id_by_lower = {r_name.lower(): r_id for r_id, r_name in root_rows}

    for name, parent_name, is_system, sort_order in SEED_CATEGORIES:
        if parent_name is None:
            continue
        parent_id = id_by_lower.get(parent_name.lower())
        amort = "annual" if name.lower() in annual_budget_cats else None
        bind.execute(
            insert_stmt,
            {
                "name": name,
                "parent_id": parent_id,
                "is_system": is_system,
                "is_active": True,
                "amortize_period": amort,
                "sort_order": sort_order,
                "created_at": now,
                "updated_at": now,
            },
        )
        seen_lower.add(name.lower())

    # 3. Preserve any existing non-seeded category names already referenced in ledger/rules
    discovered: list[str] = []
    for query_sql in (
        "SELECT DISTINCT category FROM transactions WHERE category IS NOT NULL",
        "SELECT DISTINCT category FROM budget_rules WHERE category IS NOT NULL",
        "SELECT DISTINCT target_category FROM categorization_rules WHERE target_category IS NOT NULL",
        "SELECT DISTINCT category FROM receipt_items WHERE category IS NOT NULL",
        "SELECT DISTINCT category FROM products WHERE category IS NOT NULL",
    ):
        for (raw_cat,) in bind.execute(sa.text(query_sql)).fetchall():
            cleaned = (raw_cat or "").strip()[:50]
            if cleaned and cleaned.lower() not in seen_lower:
                seen_lower.add(cleaned.lower())
                discovered.append(cleaned)

    for idx, custom_name in enumerate(sorted(discovered), start=200):
        amort = "annual" if custom_name.lower() in annual_budget_cats else None
        bind.execute(
            insert_stmt,
            {
                "name": custom_name,
                "parent_id": None,
                "is_system": False,
                "is_active": True,
                "amortize_period": amort,
                "sort_order": idx,
                "created_at": now,
                "updated_at": now,
            },
        )


def downgrade() -> None:
    op.drop_index("ix_categories_parent_id", table_name="categories")
    op.drop_index("ix_categories_name", table_name="categories")
    op.drop_index("ix_categories_id", table_name="categories")
    op.drop_table("categories")
