"""Transactions and spending router."""

from datetime import date, timedelta
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.models import Account, Loan, Receipt, Transaction
from app.schemas import (
    TagCategoryBreakdownItem,
    TagSpendingSummaryItem,
    TagSpendingSummaryResponse,
    TransactionCreate,
    TransactionRead,
    TransactionUpdate,
)
from app.services.burn_service import (
    TRANSFER_CATEGORIES,
    expand_expense_lines_by_receipt,
    to_nzd_amount,
    weekly_to_monthly,
)
from app.services.category_taxonomy import get_category_parent_map, get_root_category
from app.services.fx_service import get_exchange_rate
from app.services.period_service import resolve_period_dates
from app.services.receipt_service import reconcile_unlinked_receipts
from app.services.recurrence_service import (
    get_active_recurring_commitments,
    get_period_amortized_recurrences,
    is_recurrence_excluded_from_period,
)
from app.services.rules_engine import normalize_tags, parse_rule_tags
from app.services.statement_parser import generate_transaction_hash
from app.services.transfer_service import reconcile_transfers

router = APIRouter(prefix="/transactions", tags=["Transactions"])


def _transaction_load_options(*, include_receipt_items: bool = False):
    """Return consistent eager-load options for TransactionRead hydration."""
    receipt_load = (
        joinedload(Transaction.receipt).joinedload(Receipt.items)
        if include_receipt_items
        else joinedload(Transaction.receipt)
    )
    return (
        joinedload(Transaction.account),
        joinedload(Transaction.counterpart_account),
        joinedload(Transaction.loan),
        joinedload(Transaction.linked_transaction).joinedload(Transaction.account),
        receipt_load,
    )


def hydrate_transaction(tx: Transaction) -> Transaction:
    """Populate derived account, counterpart, loan, linked-transfer, and receipt fields."""
    tx.account_name = tx.account.name if tx.account else None
    tx.counterpart_account_name = (
        tx.counterpart_account.name if tx.counterpart_account else None
    )
    tx.loan_name = tx.loan.name if tx.loan else None
    linked = tx.linked_transaction
    if linked is not None:
        tx.linked_account_name = linked.account.name if linked.account else None
        tx.linked_amount = float(linked.amount)
        tx.linked_currency = linked.currency
    else:
        tx.linked_account_name = None
        tx.linked_amount = None
        tx.linked_currency = None
    tx.has_receipt = bool(tx.receipt)
    tx.receipt_id = tx.receipt.id if tx.receipt else None
    return tx


def _resolve_unlinked_fx_rate(db: Session, leg: Transaction) -> Optional[float]:
    """Return None for NZD or resolved currency->NZD rate for unlinked non-NZD transactions."""
    currency = (leg.currency or "NZD").strip().upper()
    if currency == "NZD":
        return None
    resolved = get_exchange_rate(
        db,
        rate_date=leg.transaction_date,
        from_currency=currency,
        to_currency="NZD",
    )
    if resolved is not None and resolved[0] > 0:
        return round(float(resolved[0]), 8)
    return None


def _matches_receipt_transaction_filter(
    tx: Transaction,
    *,
    category: Optional[str],
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
    parent_map: Optional[dict[str, str]] = None,
) -> Optional[TransactionRead]:
    """Filter a transaction by category and/or tag, expanding matched receipt items.

    Matched receipt items inherit tags from their parent transaction alongside
    their own item-level tags. Returns a full or receipt-split TransactionRead,
    or None if the transaction does not match.
    """
    tx_cat_lower = (tx.category or "General").strip().lower()
    is_expandable = (
        float(tx.amount) < 0
        and tx.receipt is not None
        and bool(tx.receipt.items)
        and tx_cat_lower not in TRANSFER_CATEGORIES
    )
    if not is_expandable:
        if target_tag and target_tag not in parse_rule_tags(tx.tags):
            return None
        if target_cat:
            effective_lower = (
                get_root_category(tx.category or "General", parent_map).lower()
                if include_subclasses
                else tx_cat_lower
            )
            if effective_lower != target_cat:
                return None
        return TransactionRead.model_validate(tx)

    lines = expand_expense_lines_by_receipt(tx)
    matched_lines = [
        line
        for line in lines
        if (not target_tag or target_tag in line.effective_tags)
        and (
            not target_cat
            or (
                get_root_category(line.category, parent_map).lower()
                if include_subclasses
                else line.category.lower()
            )
            == target_cat
        )
    ]
    if not matched_lines:
        return None

    matched_nzd = round(sum(line.amount_nzd for line in matched_lines), 2)
    total_tx_nzd = round(sum(line.amount_nzd for line in lines), 2)
    matched_cats = {line.category for line in matched_lines}
    if target_cat and include_subclasses:
        matched_cat = category.strip() if category else (tx.category or "General")
    elif len(matched_cats) == 1:
        matched_cat = next(iter(matched_cats))
    else:
        matched_cat = tx.category or "General"

    merged_tags = normalize_tags(t for line in matched_lines for t in line.effective_tags)
    is_partial = len(matched_lines) < len(lines) or abs(matched_nzd - total_tx_nzd) > 0.01
    if is_partial or (target_cat and matched_cat.lower() != tx_cat_lower):
        fx_unit = to_nzd_amount(1.0, tx.currency, tx.exchange_rate)
        split_orig = round(matched_nzd / fx_unit, 2) if fx_unit > 0 else matched_nzd
        item_names = ", ".join(line.item_name for line in matched_lines if line.item_name)
        return TransactionRead.model_validate(tx).model_copy(
            update={
                "amount": -abs(split_orig),
                "category": matched_cat,
                "tags": merged_tags,
                "is_receipt_split": True,
                "notes": (
                    f"Receipt split ({item_names}) of {tx.currency} {abs(float(tx.amount)):.2f}"
                    if item_names
                    else tx.notes
                ),
            }
        )

    return TransactionRead.model_validate(tx).model_copy(
        update={"tags": merged_tags or parse_rule_tags(tx.tags)}
    )


def _collect_amortized_commitments(
    db: Session,
    *,
    ref_date: date,
    effective_period: Optional[str],
    target_cat: str,
    include_subclasses: bool,
    account_id: Optional[int],
    amortized_recurrences: frozenset[str],
    parent_map: Optional[dict[str, str]] = None,
) -> list[TransactionRead]:
    """Build TransactionRead entries for active recurring commitments amortized into a period."""
    is_monthly = effective_period in ("month", "last_month")
    cadence = "month" if is_monthly else "week"
    amortized_results: list[TransactionRead] = []

    for rec_tx, weekly_nzd in get_active_recurring_commitments(db, ref_date, eager_load=True):
        if account_id is not None and rec_tx.account_id != account_id:
            continue
        rec = (rec_tx.recurrence or "").strip().lower()
        if rec not in amortized_recurrences:
            continue
        rec_cat = rec_tx.category or "General"
        effective_rec_lower = (
            get_root_category(rec_cat, parent_map).lower()
            if include_subclasses
            else rec_cat.strip().lower()
        )
        if effective_rec_lower != target_cat:
            continue

        hydrate_transaction(rec_tx)
        amortized_nzd = weekly_to_monthly(weekly_nzd) if is_monthly else weekly_nzd
        amortized_results.append(
            TransactionRead.model_validate(rec_tx).model_copy(
                update={
                    "is_amortized_commitment": True,
                    "amortized_period_amount_nzd": amortized_nzd,
                    "amortized_cadence": cadence,
                }
            )
        )

    amortized_results.sort(
        key=lambda item: (
            -float(item.amortized_period_amount_nzd or 0.0),
            -item.transaction_date.toordinal(),
            -item.id,
        )
    )
    return amortized_results


@router.get("", response_model=list[TransactionRead])
def list_transactions(
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    category: Optional[str] = Query(default=None),
    include_subclasses: bool = Query(default=False, description="Match child categories inheriting from category"),
    include_amortized: bool = Query(
        default=False,
        description="Include active amortized recurring commitments and flag period-excluded items when inspecting a named weekly/monthly category",
    ),
    tag: Optional[str] = Query(default=None),
    period: Optional[str] = Query(
        default=None,
        description="'week', 'last_week', 'month', 'last_month', 'annual', or 'all'",
    ),
    account_id: Optional[int] = Query(default=None),
    loan_id: Optional[int] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """List transactions with optional date range, period, account_id, loan_id, category, and tag filters."""
    effective_period = None if (start_date is not None or end_date is not None) else period
    start_date, end_date = resolve_period_dates(effective_period, start_date, end_date)
    ref_date = end_date or date.today()
    amortized_recurrences = get_period_amortized_recurrences(effective_period)

    stmt = select(Transaction).options(*_transaction_load_options(include_receipt_items=True))
    if account_id is not None:
        stmt = stmt.where(
            or_(
                Transaction.account_id == account_id,
                and_(
                    Transaction.counterpart_account_id == account_id,
                    Transaction.linked_transaction_id.is_(None),
                ),
            )
        )
    if loan_id is not None:
        stmt = stmt.where(Transaction.loan_id == loan_id)
    if start_date:
        stmt = stmt.where(Transaction.transaction_date >= start_date)
    if end_date:
        stmt = stmt.where(Transaction.transaction_date <= end_date)

    ordered_stmt = stmt.order_by(desc(Transaction.transaction_date), desc(Transaction.id))
    target_cat = category.strip().lower() if category else None
    target_tag = tag.strip().lower().lstrip("#").replace(" ", "-") if tag else None

    if target_cat or target_tag:
        parent_map = get_category_parent_map(db) if include_subclasses else None
        should_include_amortized = bool(
            include_amortized
            and target_cat
            and target_cat not in TRANSFER_CATEGORIES
            and not target_tag
            and amortized_recurrences
        )
        amortized_results = (
            _collect_amortized_commitments(
                db,
                ref_date=ref_date,
                effective_period=effective_period,
                target_cat=target_cat,
                include_subclasses=include_subclasses,
                account_id=account_id,
                amortized_recurrences=amortized_recurrences,
                parent_map=parent_map,
            )
            if should_include_amortized
            else []
        )
        amortized_tx_ids = {item.id for item in amortized_results}
        max_actual = max(0, limit - len(amortized_results)) if should_include_amortized else limit

        rows = db.scalars(ordered_stmt).unique().all()
        results: list[TransactionRead] = []
        if max_actual > 0:
            for tx in rows:
                if tx.id in amortized_tx_ids:
                    continue
                hydrate_transaction(tx)
                dto = _matches_receipt_transaction_filter(
                    tx,
                    category=category,
                    target_cat=target_cat,
                    target_tag=target_tag,
                    include_subclasses=include_subclasses,
                    parent_map=parent_map,
                )
                if dto is not None:
                    if should_include_amortized and (
                        float(tx.amount) >= 0
                        or is_recurrence_excluded_from_period(effective_period, tx.recurrence)
                    ):
                        dto = dto.model_copy(update={"excluded_from_period_budget": True})
                    results.append(dto)
                    if len(results) >= max_actual:
                        break
        if should_include_amortized:
            return results[:max_actual] + amortized_results[:limit]
        return results

    txs = db.scalars(ordered_stmt.limit(limit)).unique().all()
    for tx in txs:
        hydrate_transaction(tx)
    return txs


@router.patch("/{transaction_id}", response_model=TransactionRead)
def update_transaction(
    transaction_id: int,
    payload: TransactionUpdate,
    db: Session = Depends(get_db),
):
    """Update category, merchant, or notes of an existing transaction."""
    tx = db.scalar(
        select(Transaction)
        .options(*_transaction_load_options())
        .where(Transaction.id == transaction_id)
    )
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")

    if payload.category is not None:
        cat = payload.category.strip()
        if not cat:
            raise HTTPException(status_code=400, detail="Category name cannot be empty")
        was_transfer_or_linked = (
            (tx.category or "").strip().lower() in TRANSFER_CATEGORIES
            or tx.linked_transaction_id is not None
        )
        tx.category = cat
        if cat.lower() not in TRANSFER_CATEGORIES and was_transfer_or_linked:
            if tx.linked_transaction_id is not None:
                partner = db.get(Transaction, tx.linked_transaction_id)
                if partner is not None:
                    partner.linked_transaction_id = None
                    partner.fee_amount = None
                    partner.exchange_rate = _resolve_unlinked_fx_rate(db, partner)
            tx.linked_transaction_id = None
            tx.fee_amount = None
            tx.exchange_rate = _resolve_unlinked_fx_rate(db, tx)

    if payload.merchant is not None:
        m = payload.merchant.strip()
        if m:
            tx.merchant = m

    if payload.notes is not None:
        tx.notes = payload.notes

    if payload.tags is not None:
        tx.tags = json.dumps(normalize_tags(payload.tags))

    if "recurrence" in payload.model_fields_set:
        tx.recurrence = payload.recurrence

    if "loan_id" in payload.model_fields_set:
        if payload.loan_id is not None and db.get(Loan, payload.loan_id) is None:
            raise HTTPException(status_code=404, detail="Loan not found")
        tx.loan_id = payload.loan_id

    db.commit()
    hydrated = db.scalar(
        select(Transaction)
        .options(*_transaction_load_options())
        .where(Transaction.id == transaction_id)
    )
    return hydrate_transaction(hydrated or tx)


@router.post("", response_model=TransactionRead, status_code=status.HTTP_201_CREATED)
def create_transaction(
    payload: TransactionCreate,
    db: Session = Depends(get_db),
):
    """Record an individual transaction with deterministic hash deduplication."""
    account = db.get(Account, payload.account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    raw_hash = generate_transaction_hash(
        tx_date=payload.transaction_date,
        amount=payload.amount,
        merchant=payload.merchant,
        account_id=payload.account_id,
    )

    existing = db.scalar(
        select(Transaction)
        .options(*_transaction_load_options())
        .where(Transaction.raw_hash == raw_hash)
    )
    if existing:
        return hydrate_transaction(existing)

    tx = Transaction(
        transaction_date=payload.transaction_date,
        account_id=payload.account_id,
        amount=payload.amount,
        currency=payload.currency,
        merchant=payload.merchant,
        category=payload.category,
        raw_description=payload.raw_description or payload.merchant,
        raw_hash=raw_hash,
        linked_transaction_id=payload.linked_transaction_id,
        exchange_rate=payload.exchange_rate,
        fee_amount=payload.fee_amount,
        tags=json.dumps(normalize_tags(payload.tags or [])),
        recurrence=payload.recurrence,
        notes=payload.notes,
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)

    # Automatically reconcile any unlinked receipts and internal/cross-currency transfers
    reconcile_unlinked_receipts(db)
    reconcile_transfers(db)

    hydrated = db.scalar(
        select(Transaction)
        .options(*_transaction_load_options())
        .where(Transaction.id == tx.id)
    )
    return hydrate_transaction(hydrated or tx)


@router.post("/reconcile-transfers")
def trigger_transfer_reconciliation(db: Session = Depends(get_db)):
    """Trigger automatic transfer reconciliation across accounts."""
    matched = reconcile_transfers(db)
    return {"status": "ok", "matched_transfers": matched}


@router.get("/summary/categories")
def get_category_summary(
    start_date: date = Query(...),
    end_date: date = Query(...),
    db: Session = Depends(get_db),
):
    """Get aggregated spending by category for a given date window."""
    stmt = (
        select(
            Transaction.category,
            func.sum(Transaction.amount).label("total_amount"),
            func.count(Transaction.id).label("count"),
        )
        .where(
            Transaction.transaction_date >= start_date,
            Transaction.transaction_date <= end_date,
        )
        .group_by(Transaction.category)
        .order_by(func.sum(Transaction.amount))
    )
    results = db.execute(stmt).all()
    return [
        {
            "category": row.category,
            "total_amount": float(row.total_amount or 0.0),
            "count": row.count,
        }
        for row in results
    ]


@router.get("/summary/tags", response_model=TagSpendingSummaryResponse)
def get_tag_spending_summary(
    period: Optional[str] = Query(
        default="all",
        description="'week', 'last_week', 'month', 'last_month', 'annual', or 'all'",
    ),
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    db: Session = Depends(get_db),
):
    """Aggregate NZD expense totals and per-category breakdowns for every tag.

    Matched receipt items inherit tags from their parent transaction and combine
    them with any item-level tags so multi-category receipts split cleanly by tag.
    """
    effective_period = None if (start_date is not None or end_date is not None) else period
    start_date, end_date = resolve_period_dates(effective_period, start_date, end_date)

    stmt = (
        select(Transaction)
        .options(joinedload(Transaction.receipt).joinedload(Receipt.items))
        .where(
            Transaction.amount < 0,
            func.lower(Transaction.category).not_in(TRANSFER_CATEGORIES),
        )
    )
    if start_date:
        stmt = stmt.where(Transaction.transaction_date >= start_date)
    if end_date:
        stmt = stmt.where(Transaction.transaction_date <= end_date)

    rows = (
        db.scalars(stmt.order_by(desc(Transaction.transaction_date), desc(Transaction.id)))
        .unique()
        .all()
    )

    total_tagged_spend = 0.0
    tag_map: dict[str, dict] = {}
    for tx in rows:
        for line in expand_expense_lines_by_receipt(tx):
            if not line.effective_tags:
                continue
            total_tagged_spend += line.amount_nzd

            for tag_key in line.effective_tags:
                bucket = tag_map.setdefault(
                    tag_key,
                    {
                        "spend": 0.0,
                        "tx_ids": set(),
                        "first_date": tx.transaction_date,
                        "last_date": tx.transaction_date,
                        "categories": {},
                    },
                )
                bucket["spend"] += line.amount_nzd
                bucket["tx_ids"].add(tx.id)
                if tx.transaction_date < bucket["first_date"]:
                    bucket["first_date"] = tx.transaction_date
                if tx.transaction_date > bucket["last_date"]:
                    bucket["last_date"] = tx.transaction_date

                cat_bucket = bucket["categories"].setdefault(
                    line.category, {"spend": 0.0, "tx_ids": set()}
                )
                cat_bucket["spend"] += line.amount_nzd
                cat_bucket["tx_ids"].add(tx.id)

    tag_items: list[TagSpendingSummaryItem] = []
    for tag_key, bucket in tag_map.items():
        tag_total = round(bucket["spend"], 2)
        cat_items = [
            TagCategoryBreakdownItem(
                category=cat_name,
                total_spend=round(cat_data["spend"], 2),
                transaction_count=len(cat_data["tx_ids"]),
                percentage=round(cat_data["spend"] / tag_total * 100.0, 1) if tag_total > 0 else 0.0,
            )
            for cat_name, cat_data in bucket["categories"].items()
        ]
        cat_items.sort(key=lambda item: (-item.total_spend, item.category))
        tag_items.append(
            TagSpendingSummaryItem(
                tag=tag_key,
                total_spend=tag_total,
                transaction_count=len(bucket["tx_ids"]),
                first_date=bucket["first_date"],
                last_date=bucket["last_date"],
                categories=cat_items,
            )
        )

    tag_items.sort(key=lambda item: (-item.total_spend, item.tag))
    return TagSpendingSummaryResponse(
        period=period or "custom",
        start_date=start_date,
        end_date=end_date,
        total_tagged_spend=round(total_tagged_spend, 2),
        tags=tag_items,
    )
