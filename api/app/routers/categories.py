"""Thin HTTP router for hierarchical spending categories and 52-week amortization."""

from typing import NoReturn, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import (
    CategoryCreate,
    CategoryDeleteResult,
    CategoryRead,
    CategoryUpdate,
)
from app.services.category_taxonomy import (
    UNSET,
    CategoryConflictError,
    CategoryCreateInput,
    CategoryDeleteSummary,
    CategoryNotFoundError,
    CategorySnapshot,
    CategoryTaxonomyError,
    CategoryUpdateInput,
    create_category as create_category_record,
    delete_category as delete_category_record,
    list_categories as list_category_snapshots,
    update_category as update_category_record,
)

router = APIRouter(prefix="/categories", tags=["Categories & Hierarchy"])

_EXCEPTION_STATUS_MAP: tuple[tuple[type[CategoryTaxonomyError], int], ...] = (
    (CategoryNotFoundError, status.HTTP_404_NOT_FOUND),
    (CategoryConflictError, status.HTTP_409_CONFLICT),
)


def _raise_http_error(exc: CategoryTaxonomyError) -> NoReturn:
    for exc_type, http_status in _EXCEPTION_STATUS_MAP:
        if isinstance(exc, exc_type):
            raise HTTPException(status_code=http_status, detail=str(exc)) from exc
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _to_category_read(snapshot: CategorySnapshot) -> CategoryRead:
    return CategoryRead(
        id=snapshot.id,
        name=snapshot.name,
        parent_id=snapshot.parent_id,
        parent_name=snapshot.parent_name,
        is_system=snapshot.is_system,
        is_active=snapshot.is_active,
        amortize_period=snapshot.amortize_period,
        sort_order=snapshot.sort_order,
        transaction_count=snapshot.transaction_count,
        rule_count=snapshot.rule_count,
        child_count=snapshot.child_count,
        has_budget_rule=snapshot.has_budget_rule,
        budget_period=snapshot.budget_period,
        weekly_limit=snapshot.weekly_limit,
        monthly_limit=snapshot.monthly_limit,
        annual_limit=snapshot.annual_limit,
    )


def _to_delete_result(summary: CategoryDeleteSummary) -> CategoryDeleteResult:
    return CategoryDeleteResult(
        deleted_id=summary.deleted_id,
        deleted_name=summary.deleted_name,
        reassigned_to=summary.reassigned_to,
        transactions_reassigned=summary.transactions_reassigned,
        splits_reassigned=summary.splits_reassigned,
        rules_reassigned=summary.rules_reassigned,
        receipt_items_reassigned=summary.receipt_items_reassigned,
        products_reassigned=summary.products_reassigned,
        children_promoted=summary.children_promoted,
    )


@router.get("", response_model=list[CategoryRead])
def list_categories(
    include_inactive: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    """List hierarchical categories ordered by base class then nested sub-categories."""
    snapshots = list_category_snapshots(db, include_inactive=include_inactive)
    return [_to_category_read(snapshot) for snapshot in snapshots]


@router.post("", response_model=CategoryRead, status_code=status.HTTP_201_CREATED)
def create_category(
    payload: CategoryCreate,
    db: Session = Depends(get_db),
):
    """Create a new base category or sub-category with optional 52-week annual amortization."""
    command = CategoryCreateInput(
        name=payload.name,
        parent_id=payload.parent_id,
        amortize_period=payload.amortize_period,
        sort_order=payload.sort_order,
    )
    try:
        snapshot = create_category_record(db, command)
    except CategoryTaxonomyError as exc:
        _raise_http_error(exc)
    return _to_category_read(snapshot)


@router.patch("/{category_id}", response_model=CategoryRead)
def update_category(
    category_id: int,
    payload: CategoryUpdate,
    db: Session = Depends(get_db),
):
    """Rename, reparent, or toggle 52-week amortization on an existing category."""
    fields = payload.model_fields_set
    command = CategoryUpdateInput(
        name=payload.name if "name" in fields else None,
        parent_id=payload.parent_id if "parent_id" in fields else UNSET,
        amortize_period=payload.amortize_period if "amortize_period" in fields else UNSET,
        is_active=payload.is_active if "is_active" in fields else None,
        sort_order=payload.sort_order if "sort_order" in fields else None,
    )
    try:
        snapshot = update_category_record(db, category_id, command)
    except CategoryTaxonomyError as exc:
        _raise_http_error(exc)
    return _to_category_read(snapshot)


@router.delete("/{category_id}", response_model=CategoryDeleteResult)
def delete_category(
    category_id: int,
    reassign_to: Optional[str] = Query(
        default=None,
        description="Target category name for existing transactions and rules (defaults to parent or General)",
    ),
    db: Session = Depends(get_db),
):
    """Delete a non-system category and safely reassign linked transactions, splits, rules, and items."""
    try:
        summary = delete_category_record(db, category_id, reassign_to=reassign_to)
    except CategoryTaxonomyError as exc:
        _raise_http_error(exc)
    return _to_delete_result(summary)
