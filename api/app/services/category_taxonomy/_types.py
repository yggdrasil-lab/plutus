"""Pure domain constants, exceptions, and immutable contracts for Category Taxonomy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


class CategoryTaxonomyError(ValueError):
    """Base domain exception for category taxonomy operations."""


class CategoryNotFoundError(CategoryTaxonomyError):
    """Raised when a target category or parent category cannot be found."""


class CategoryConflictError(CategoryTaxonomyError):
    """Raised when a category name collides case-insensitively with an existing row."""


class CategoryValidationError(CategoryTaxonomyError):
    """Raised when a taxonomy invariant, depth limit, or system guard is violated."""


class _UnsetType:
    """Sentinel distinguishing omitted update fields from explicit None values."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _UnsetType()


@dataclass(frozen=True)
class CategorySeedSpec:
    """Default hierarchical category definition."""

    name: str
    parent: Optional[str] = None
    is_system: bool = False
    sort_order: int = 100


DEFAULT_CATEGORY_SPECS: tuple[CategorySeedSpec, ...] = (
    CategorySeedSpec("Food", None, False, 10),
    CategorySeedSpec("Dining", "Food", False, 11),
    CategorySeedSpec("Groceries", "Food", False, 12),
    CategorySeedSpec("Alcohol", "Food", False, 13),
    CategorySeedSpec("Housing", None, False, 20),
    CategorySeedSpec("Rent", "Housing", False, 21),
    CategorySeedSpec("Household", "Housing", False, 22),
    CategorySeedSpec("Transport", None, False, 30),
    CategorySeedSpec("Fuel", "Transport", False, 31),
    CategorySeedSpec("Transit", "Transport", False, 32),
    CategorySeedSpec("Vehicle", "Transport", False, 33),
    CategorySeedSpec("Health", None, False, 40),
    CategorySeedSpec("Fitness", "Health", False, 41),
    CategorySeedSpec("Medical", "Health", False, 42),
    CategorySeedSpec("Supplements", "Health", False, 43),
    CategorySeedSpec("Personal Care", "Health", False, 44),
    CategorySeedSpec("Shopping", None, False, 50),
    CategorySeedSpec("Clothing", "Shopping", False, 51),
    CategorySeedSpec("Tech", "Shopping", False, 52),
    CategorySeedSpec("Utilities", None, False, 60),
    CategorySeedSpec("Bills & Telco", "Utilities", False, 61),
    CategorySeedSpec("Software & AI", "Utilities", False, 62),
    CategorySeedSpec("Insurance", None, False, 70),
    CategorySeedSpec("Leisure", None, False, 80),
    CategorySeedSpec("Gaming", "Leisure", False, 81),
    CategorySeedSpec("Travel", None, False, 90),
    CategorySeedSpec("Family", None, False, 100),
    CategorySeedSpec("Finance", None, False, 110),
    CategorySeedSpec("Fees & Admin", "Finance", False, 111),
    CategorySeedSpec("Debt & Loans", "Finance", True, 112),
    CategorySeedSpec("General", None, True, 900),
    CategorySeedSpec("Transfers", None, True, 910),
    CategorySeedSpec("Income", None, True, 920),
)

CATEGORY_PARENTS: dict[str, str] = {
    spec.name: spec.parent for spec in DEFAULT_CATEGORY_SPECS if spec.parent is not None
}
DEFAULT_CATEGORY_NAMES: tuple[str, ...] = tuple(spec.name for spec in DEFAULT_CATEGORY_SPECS)
SYSTEM_CATEGORY_NAMES: frozenset[str] = frozenset(
    spec.name for spec in DEFAULT_CATEGORY_SPECS if spec.is_system
)
SYSTEM_CATEGORY_NAMES_LOWER: frozenset[str] = frozenset(
    name.lower() for name in SYSTEM_CATEGORY_NAMES
)
_NON_EXPENSE_SYSTEM_LOWER: frozenset[str] = frozenset({"transfers", "transfer", "income"})
_VALID_AMORTIZE_PERIODS: frozenset[str] = frozenset({"annual"})
_MAX_CATEGORY_NAME_LENGTH = 50


@dataclass(frozen=True)
class CategoryMetrics:
    """Detached usage metrics aggregated across taxonomy and denormalized ledger tables."""

    transaction_count: int = 0
    rule_count: int = 0
    child_count: int = 0
    receipt_item_count: int = 0
    product_count: int = 0


@dataclass(frozen=True)
class CategorySnapshot:
    """Detached immutable view of a category, its metrics, and active budget state."""

    id: int
    name: str
    parent_id: Optional[int] = None
    parent_name: Optional[str] = None
    is_system: bool = False
    is_active: bool = True
    amortize_period: Optional[str] = None
    sort_order: int = 100
    has_budget_rule: bool = False
    budget_period: Optional[str] = None
    weekly_limit: Optional[float] = None
    monthly_limit: Optional[float] = None
    annual_limit: Optional[float] = None
    metrics: CategoryMetrics = CategoryMetrics()

    @property
    def transaction_count(self) -> int:
        return self.metrics.transaction_count

    @property
    def rule_count(self) -> int:
        return self.metrics.rule_count

    @property
    def child_count(self) -> int:
        return self.metrics.child_count

    @property
    def receipt_item_count(self) -> int:
        return self.metrics.receipt_item_count

    @property
    def product_count(self) -> int:
        return self.metrics.product_count


@dataclass(frozen=True)
class CategoryDeleteSummary:
    """Detached immutable summary of reassignment counts after deleting a category."""

    deleted_id: int
    deleted_name: str
    reassigned_to: str
    transactions_reassigned: int = 0
    splits_reassigned: int = 0
    rules_reassigned: int = 0
    receipt_items_reassigned: int = 0
    products_reassigned: int = 0
    children_promoted: int = 0


@dataclass(frozen=True)
class CategoryCreateInput:
    """Typed input contract for creating a category."""

    name: str
    parent_id: Optional[int] = None
    amortize_period: Optional[str] = None
    sort_order: Optional[int] = None


@dataclass(frozen=True)
class CategoryUpdateInput:
    """Typed input contract for updating a category."""

    name: Optional[str] = None
    parent_id: int | None | _UnsetType = UNSET
    amortize_period: str | None | _UnsetType = UNSET
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None


def is_system_category_name(name: str) -> bool:
    """Return True if the given category name matches a reserved system category."""
    return (name or "").strip().lower() in SYSTEM_CATEGORY_NAMES_LOWER


def normalize_category_name(name: str) -> str:
    """Validate and trim a category name."""
    cleaned = (name or "").strip()
    if not cleaned:
        raise CategoryValidationError("Category name cannot be empty.")
    if len(cleaned) > _MAX_CATEGORY_NAME_LENGTH:
        raise CategoryValidationError(
            f"Category name must be {_MAX_CATEGORY_NAME_LENGTH} characters or fewer."
        )
    return cleaned


def normalize_amortize_setting(value: Optional[str]) -> Optional[str]:
    """Validate and normalize a category amortization setting."""
    if value is None:
        return None
    cleaned = str(value).strip().lower()
    if not cleaned or cleaned in ("none", "direct", "variable"):
        return None
    if cleaned not in _VALID_AMORTIZE_PERIODS:
        raise CategoryValidationError(
            f"Unsupported amortize_period '{value}'. Must be 'annual' or null."
        )
    return cleaned
