"""Deep Product Catalog package for CRUD, linking, and batched price telemetry."""

from app.services.product_catalog._types import (
    UNSET,
    PricePointSnapshot,
    ProductCatalogError,
    ProductConflictError,
    ProductCreateInput,
    ProductListQuery,
    ProductListResult,
    ProductNotFoundError,
    ProductRef,
    ProductSnapshot,
    ProductStatisticSnapshot,
    ProductStatisticsSnapshot,
    ProductUpdateInput,
    ProductValidationError,
)
from app.services.product_catalog.lifecycle import (
    backfill_unlinked_receipt_item_products,
    create_product,
    delete_product,
    get_or_create_product,
    update_product,
)
from app.services.product_catalog.queries import (
    compute_product_statistics,
    get_product_snapshot,
    list_products,
)

__all__ = [
    "PricePointSnapshot",
    "ProductCatalogError",
    "ProductConflictError",
    "ProductCreateInput",
    "ProductListQuery",
    "ProductListResult",
    "ProductNotFoundError",
    "ProductRef",
    "ProductSnapshot",
    "ProductStatisticSnapshot",
    "ProductStatisticsSnapshot",
    "ProductUpdateInput",
    "ProductValidationError",
    "UNSET",
    "backfill_unlinked_receipt_item_products",
    "compute_product_statistics",
    "create_product",
    "delete_product",
    "get_or_create_product",
    "get_product_snapshot",
    "list_products",
    "update_product",
]
