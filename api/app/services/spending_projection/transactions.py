"""Transaction-level cashflow and ledger listing projection."""

from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Transaction, TransactionSplit
from app.services.rules_engine import normalize_tags, parse_rule_tags
from app.services.spending_projection._lines import (
    _is_lent_loan,
    _iter_active_recurring_allocations,
    _load_annual_metadata,
    _nzd_to_tx_currency,
    _resolve_unsplit_inflow_nzd,
    _snapshot_transaction,
    matches_category_subtree,
    scale_receipt_items,
)
from app.services.spending_projection._types import (
    TRANSFER_CATEGORIES,
    ExpenseLine,
    FlowLiteral,
    InvalidWindowError,
    ProjectedTransaction,
    RecurringCommitmentAllocation,
    ReportingWindow,
    TransactionProjection,
    TransactionQuery,
    _AnnualCategoryMetadata,
)
from app.services.spending_projection._window import (
    _query_paginated_window_transactions,
    _query_window_transactions,
    is_recurrence_excluded_from_period,
    resolve_reporting_window,
)
from app.services.fx_service import to_nzd_amount


def _matches_account_filter(tx: Transaction, account_id: Optional[int]) -> bool:
    """Return True when transaction belongs to account_id directly or as unlinked counterpart."""
    if account_id is None:
        return True
    return tx.account_id == account_id or (
        tx.counterpart_account_id == account_id and tx.linked_transaction_id is None
    )


def _matches_loan_filter(tx: Transaction, loan_id: Optional[int]) -> bool:
    """Return True when transaction or any of its splits links to loan_id."""
    if loan_id is None:
        return True
    if tx.loan_id == loan_id:
        return True
    return any(s.loan_id == loan_id for s in (getattr(tx, "splits", None) or ()))


def _resolve_projected_category(
    tx_cat: str,
    matched_cats: set[str],
    category: Optional[str],
    target_cat: Optional[str],
    include_subclasses: bool,
) -> str:
    """Resolve display category for a projected split or receipt slice."""
    if target_cat and include_subclasses:
        return category.strip() if category else tx_cat
    if len(matched_cats) == 1:
        return next(iter(matched_cats))
    if tx_cat.lower() in TRANSFER_CATEGORIES and matched_cats:
        return sorted(matched_cats)[0]
    return tx_cat


def _build_projected_split_snapshot(
    tx: Transaction,
    tx_cat: str,
    parent_tags: tuple[str, ...],
    matched: list[tuple[TransactionSplit, str, tuple[str, ...], float]],
    *,
    category: Optional[str],
    target_cat: Optional[str],
    include_subclasses: bool,
    excluded: bool = False,
) -> ProjectedTransaction:
    """Build a detached ProjectedTransaction from matching Manual Split lines on one transaction."""
    matched_orig = round(sum(amt for _, _, _, amt in matched), 2)
    total_orig = round(sum(s.signed_amount() for s in tx.splits), 2)
    matched_cat = _resolve_projected_category(
        tx_cat, {c for _, c, _, _ in matched}, category, target_cat, include_subclasses
    )
    merged_tags = tuple(normalize_tags(t for _, _, tags, _ in matched for t in tags))
    is_partial = len(matched) < len(tx.splits) or abs(matched_orig - total_orig) > 0.01
    if is_partial or (target_cat and matched_cat.lower() != tx_cat.lower()):
        split_notes = ", ".join(s.notes for s, _, _, _ in matched if s.notes)
        fallback = f"Split of {tx.currency} {abs(float(tx.amount)):.2f}" if is_partial else tx.notes
        return _snapshot_transaction(
            tx,
            amount_override=matched_orig,
            category_override=matched_cat,
            tags_override=merged_tags,
            notes_override=split_notes or fallback,
            is_split_override=True,
            excluded_from_period_budget=excluded,
        )
    resolved_cat = matched_cat if matched_cat != tx_cat else None
    return _snapshot_transaction(
        tx,
        category_override=resolved_cat,
        tags_override=merged_tags or parent_tags,
        is_split_override=len(tx.splits) >= 2,
        excluded_from_period_budget=excluded,
    )


def _build_receipt_outflow_snapshot(
    tx: Transaction,
    tx_cat: str,
    parent_tags: tuple[str, ...],
    tx_nzd: float,
    positive_count: int,
    matched_lines: list[ExpenseLine],
    *,
    category: Optional[str],
    target_cat: Optional[str],
    include_subclasses: bool,
    excluded: bool,
) -> ProjectedTransaction:
    """Build a detached ProjectedTransaction from matching scaled Receipt Lines."""
    matched_nzd = round(sum(line.amount_nzd for line in matched_lines), 2)
    matched_cat = _resolve_projected_category(
        tx_cat, {line.category for line in matched_lines}, category, target_cat, include_subclasses
    )
    merged_tags = tuple(normalize_tags(t for line in matched_lines for t in line.effective_tags))
    is_partial = len(matched_lines) < positive_count or abs(matched_nzd - tx_nzd) > 0.01
    if is_partial or (target_cat and matched_cat.lower() != tx_cat.lower()):
        names = ", ".join(line.item_name for line in matched_lines if line.item_name)
        notes = f"Receipt split ({names}) of {tx.currency} {abs(float(tx.amount)):.2f}" if names else tx.notes
        return _snapshot_transaction(
            tx,
            amount_override=-abs(_nzd_to_tx_currency(matched_nzd, tx)),
            category_override=matched_cat,
            tags_override=merged_tags,
            notes_override=notes,
            is_receipt_split=True,
            excluded_from_period_budget=excluded,
        )
    return _snapshot_transaction(
        tx,
        tags_override=merged_tags or parent_tags,
        excluded_from_period_budget=excluded,
    )


def _project_inflow_splits(
    tx: Transaction,
    tx_cat: str,
    parent_tags: tuple[str, ...],
    lower_to_parent: dict[str, str],
    *,
    category: Optional[str],
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
) -> list[ProjectedTransaction]:
    """Project positive non-Transfer, non-lent Manual Split lines for flow='inflow'."""
    matched: list[tuple[TransactionSplit, str, tuple[str, ...], float]] = []
    for s in tx.splits:
        signed_amt = s.signed_amount()
        if signed_amt <= 0 or _is_lent_loan(getattr(s, "loan", None)):
            continue
        s_cat = (s.category or tx_cat).strip() or "General"
        if s_cat.lower() in TRANSFER_CATEGORIES:
            continue
        s_tags = tuple(normalize_tags([*parent_tags, *parse_rule_tags(s.tags)]))
        if target_tag and target_tag not in s_tags:
            continue
        if not matches_category_subtree(s_cat, target_cat, include_subclasses, lower_to_parent):
            continue
        matched.append((s, s_cat, s_tags, signed_amt))
    if not matched:
        return []
    return [
        _build_projected_split_snapshot(
            tx, tx_cat, parent_tags, matched,
            category=category, target_cat=target_cat, include_subclasses=include_subclasses,
        )
    ]


def _project_unsplit_inflow(
    tx: Transaction,
    tx_cat: str,
    parent_tags: tuple[str, ...],
    lower_to_parent: dict[str, str],
    *,
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
) -> list[ProjectedTransaction]:
    """Project an unsplit positive Reporting Currency inflow excluding Transfers and lent principal."""
    amt = float(tx.amount)
    if amt <= 0 or tx_cat.lower() in TRANSFER_CATEGORIES:
        return []
    is_lent = _is_lent_loan(getattr(tx, "loan", None))
    amt_nzd = to_nzd_amount(amt, tx.currency, tx.exchange_rate)
    net_nzd = _resolve_unsplit_inflow_nzd(tx, amt_nzd, is_lent=is_lent)
    if net_nzd <= 0:
        return []
    if target_tag and target_tag not in parent_tags:
        return []
    if not matches_category_subtree(tx_cat, target_cat, include_subclasses, lower_to_parent):
        return []
    projected_amt = (
        _nzd_to_tx_currency(net_nzd, tx)
        if (is_lent and tx.loan_payment_amount is not None)
        else None
    )
    return [_snapshot_transaction(tx, amount_override=projected_amt)]


def _classify_outflow_splits(
    tx: Transaction,
    tx_cat: str,
    parent_tags: tuple[str, ...],
    meta: _AnnualCategoryMetadata,
    *,
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
) -> tuple[
    list[tuple[TransactionSplit, str, tuple[str, ...], float]],
    list[tuple[TransactionSplit, str, tuple[str, ...], float]],
]:
    """Filter negative non-Transfer splits and partition into active vs excluded."""
    active: list[tuple[TransactionSplit, str, tuple[str, ...], float]] = []
    excluded: list[tuple[TransactionSplit, str, tuple[str, ...], float]] = []
    is_rec_excluded = is_recurrence_excluded_from_period(
        meta.resolved.effective_period, tx.recurrence
    )
    for s in tx.splits:
        signed_amt = s.signed_amount()
        if signed_amt >= 0:
            continue
        s_cat = (s.category or tx_cat).strip() or "General"
        if s_cat.lower() in TRANSFER_CATEGORIES:
            continue
        s_tags = tuple(normalize_tags([*parent_tags, *parse_rule_tags(s.tags)]))
        if target_tag and target_tag not in s_tags:
            continue
        if not matches_category_subtree(s_cat, target_cat, include_subclasses, meta.lower_to_parent):
            continue
        is_excluded = (
            _is_lent_loan(getattr(s, "loan", None))
            or is_rec_excluded
            or meta.is_smoothed_annual(s_cat.lower(), meta.resolve_category(s_cat).lower())
        )
        (excluded if is_excluded else active).append((s, s_cat, s_tags, signed_amt))
    return active, excluded


def _project_outflow_splits(
    tx: Transaction,
    tx_cat: str,
    parent_tags: tuple[str, ...],
    meta: _AnnualCategoryMetadata,
    *,
    category: Optional[str],
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
    include_amortized: bool,
) -> list[ProjectedTransaction]:
    """Project negative non-Transfer Manual Split lines partitioned by active vs excluded."""
    active, excluded = _classify_outflow_splits(
        tx, tx_cat, parent_tags, meta,
        target_cat=target_cat, target_tag=target_tag, include_subclasses=include_subclasses,
    )
    items: list[ProjectedTransaction] = []
    if active:
        items.append(
            _build_projected_split_snapshot(
                tx, tx_cat, parent_tags, active, category=category,
                target_cat=target_cat, include_subclasses=include_subclasses, excluded=False,
            )
        )
    if include_amortized and excluded:
        items.append(
            _build_projected_split_snapshot(
                tx, tx_cat, parent_tags, excluded, category=category,
                target_cat=target_cat, include_subclasses=include_subclasses, excluded=True,
            )
        )
    return items


def _project_lent_unsplit_outflow(
    tx: Transaction,
    tx_cat: str,
    parent_tags: tuple[str, ...],
    lower_to_parent: dict[str, str],
    *,
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
    include_amortized: bool,
) -> list[ProjectedTransaction]:
    """Project a lent loan outflow transaction as excluded when include_amortized is enabled."""
    if not include_amortized:
        return []
    if target_tag and target_tag not in parent_tags:
        return []
    if not matches_category_subtree(tx_cat, target_cat, include_subclasses, lower_to_parent):
        return []
    return [_snapshot_transaction(tx, excluded_from_period_budget=True)]


def _project_parent_outflow(
    tx: Transaction,
    tx_cat: str,
    parent_tags: tuple[str, ...],
    meta: _AnnualCategoryMetadata,
    *,
    is_rec_excluded: bool,
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
    include_amortized: bool,
) -> list[ProjectedTransaction]:
    """Project an unsplit non-receipt outflow transaction."""
    if target_tag and target_tag not in parent_tags:
        return []
    if not matches_category_subtree(tx_cat, target_cat, include_subclasses, meta.lower_to_parent):
        return []
    is_excluded = is_rec_excluded or meta.is_smoothed_annual(
        tx_cat.lower(), meta.resolve_category(tx_cat).lower()
    )
    if is_excluded and not include_amortized:
        return []
    return [_snapshot_transaction(tx, excluded_from_period_budget=is_excluded)]


def _partition_receipt_outflow_lines(
    tx: Transaction,
    tx_cat: str,
    parent_tags: tuple[str, ...],
    meta: _AnnualCategoryMetadata,
    tx_nzd: float,
    positive_count: int,
    scaled_lines: list[ExpenseLine],
    *,
    is_rec_excluded: bool,
    category: Optional[str],
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
    include_amortized: bool,
) -> list[ProjectedTransaction]:
    """Filter scaled Receipt Lines and emit active and optional excluded slices."""
    active: list[ExpenseLine] = []
    excluded: list[ExpenseLine] = []
    for line in scaled_lines:
        if target_tag and target_tag not in line.effective_tags:
            continue
        if not matches_category_subtree(line.category, target_cat, include_subclasses, meta.lower_to_parent):
            continue
        is_excluded = is_rec_excluded or meta.is_smoothed_annual(
            line.category.lower(), meta.resolve_category(line.category).lower()
        )
        (excluded if is_excluded else active).append(line)

    slices = [(active, False)] + ([(excluded, True)] if include_amortized and excluded else [])
    return [
        _build_receipt_outflow_snapshot(
            tx, tx_cat, parent_tags, tx_nzd, positive_count, grp,
            category=category, target_cat=target_cat,
            include_subclasses=include_subclasses, excluded=is_excl,
        )
        for grp, is_excl in slices if grp
    ]


def _project_outflow_receipt_or_parent(
    tx: Transaction,
    tx_cat: str,
    parent_tags: tuple[str, ...],
    meta: _AnnualCategoryMetadata,
    *,
    category: Optional[str],
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
    include_amortized: bool,
) -> list[ProjectedTransaction]:
    """Project an unsplit outflow via Receipt Lines (excluding Transfers) or parent tx."""
    if float(tx.amount) >= 0 or tx_cat.lower() in TRANSFER_CATEGORIES:
        return []
    if _is_lent_loan(getattr(tx, "loan", None)):
        return _project_lent_unsplit_outflow(
            tx, tx_cat, parent_tags, meta.lower_to_parent,
            target_cat=target_cat, target_tag=target_tag,
            include_subclasses=include_subclasses, include_amortized=include_amortized,
        )

    scaled_lines, positive_count, tx_nzd = scale_receipt_items(
        tx, tx_cat, parent_tags, exclude_transfers=True
    )
    is_rec_excluded = is_recurrence_excluded_from_period(meta.resolved.effective_period, tx.recurrence)
    if positive_count > 0:
        return _partition_receipt_outflow_lines(
            tx, tx_cat, parent_tags, meta, tx_nzd, positive_count, scaled_lines,
            is_rec_excluded=is_rec_excluded, category=category, target_cat=target_cat,
            target_tag=target_tag, include_subclasses=include_subclasses,
            include_amortized=include_amortized,
        )
    return _project_parent_outflow(
        tx, tx_cat, parent_tags, meta, is_rec_excluded=is_rec_excluded,
        target_cat=target_cat, target_tag=target_tag,
        include_subclasses=include_subclasses, include_amortized=include_amortized,
    )


def _extract_split_tags_for_category(tx: Transaction, target_category: str) -> tuple[str, ...]:
    """Combine parent transaction tags with negative split tags matching target_category."""
    tx_cat = (tx.category or "General").strip() or "General"
    cat_lower = target_category.strip().lower()
    tags = list(parse_rule_tags(tx.tags))
    for s in getattr(tx, "splits", None) or ():
        s_cat = (s.category or tx_cat).strip().lower()
        if s.signed_amount() < 0 and s_cat == cat_lower:
            tags.extend(parse_rule_tags(s.tags))
    return tuple(normalize_tags(tags))


def _build_amortized_commitment_snapshot(
    rec_tx: Transaction,
    alloc: RecurringCommitmentAllocation,
    is_split_slice: bool,
    meta: _AnnualCategoryMetadata,
    *,
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
    account_id: Optional[int],
    loan_id: Optional[int],
) -> Optional[ProjectedTransaction]:
    """Convert an active RecurringCommitmentAllocation into ProjectedTransaction if filters match."""
    if alloc.recurrence not in meta.resolved.amortized_recurrences:
        return None
    if not _matches_account_filter(rec_tx, account_id) or not _matches_loan_filter(rec_tx, loan_id):
        return None
    if meta.is_smoothed_annual(alloc.category.lower(), meta.resolve_category(alloc.category).lower()):
        return None
    if not matches_category_subtree(alloc.category, target_cat, include_subclasses, meta.lower_to_parent):
        return None
    effective_tags = (
        _extract_split_tags_for_category(rec_tx, alloc.category)
        if is_split_slice else tuple(parse_rule_tags(rec_tx.tags))
    )
    if target_tag and target_tag not in effective_tags:
        return None
    cadence = "month" if meta.resolved.is_monthly_cadence else "week"
    amortized_nzd = alloc.monthly_nzd if meta.resolved.is_monthly_cadence else alloc.weekly_nzd
    return _snapshot_transaction(
        rec_tx,
        amount_override=-abs(alloc.original_amount) if is_split_slice else None,
        category_override=alloc.category if is_split_slice else None,
        tags_override=effective_tags if is_split_slice else None,
        is_split_override=True if is_split_slice else None,
        is_amortized_commitment=True,
        amortized_period_amount_nzd=amortized_nzd,
        amortized_cadence=cadence,
    )


def _collect_amortized_commitments(
    db: Session,
    meta: _AnnualCategoryMetadata,
    *,
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
    account_id: Optional[int],
    loan_id: Optional[int],
) -> list[ProjectedTransaction]:
    """Collect active amortized Recurring Commitments aligned with project_window."""
    if not meta.resolved.amortized_recurrences:
        return []
    items: list[ProjectedTransaction] = []
    for rec_tx, alloc, is_split_slice in _iter_active_recurring_allocations(
        db, meta.resolved.as_of_date, include_relations=True
    ):
        built = _build_amortized_commitment_snapshot(
            rec_tx, alloc, is_split_slice, meta,
            target_cat=target_cat, target_tag=target_tag,
            include_subclasses=include_subclasses, account_id=account_id, loan_id=loan_id,
        )
        if built is not None:
            items.append(built)
    items.sort(
        key=lambda item: (
            -float(item.amortized_period_amount_nzd or 0.0),
            -item.transaction_date.toordinal(),
            -item.id,
            item.category.lower(),
        )
    )
    return items


def _project_cashflow_row(
    tx: Transaction,
    meta: _AnnualCategoryMetadata,
    *,
    flow: FlowLiteral,
    category: Optional[str],
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
    include_amortized: bool,
) -> list[ProjectedTransaction]:
    """Project one Transaction into zero, one, or partitioned cashflow snapshots."""
    tx_cat = (tx.category or "General").strip() or "General"
    parent_tags = tuple(parse_rule_tags(tx.tags))
    has_splits = bool(getattr(tx, "splits", None))
    if flow == "inflow":
        if has_splits:
            return _project_inflow_splits(
                tx, tx_cat, parent_tags, meta.lower_to_parent, category=category,
                target_cat=target_cat, target_tag=target_tag, include_subclasses=include_subclasses,
            )
        return _project_unsplit_inflow(
            tx, tx_cat, parent_tags, meta.lower_to_parent, target_cat=target_cat,
            target_tag=target_tag, include_subclasses=include_subclasses,
        )
    if has_splits:
        return _project_outflow_splits(
            tx, tx_cat, parent_tags, meta, category=category, target_cat=target_cat,
            target_tag=target_tag, include_subclasses=include_subclasses,
            include_amortized=include_amortized,
        )
    return _project_outflow_receipt_or_parent(
        tx, tx_cat, parent_tags, meta, category=category, target_cat=target_cat,
        target_tag=target_tag, include_subclasses=include_subclasses,
        include_amortized=include_amortized,
    )


def _is_ledger_line_excluded(
    cat: str,
    signed_amt: float,
    recurrence: Optional[str],
    *,
    is_lent: bool,
    meta: _AnnualCategoryMetadata,
    allow_income: bool,
) -> bool:
    """Return True if a ledger line is excluded from the active period budget."""
    cat_lower = (cat or "General").strip().lower()
    if cat_lower in TRANSFER_CATEGORIES:
        return False
    if signed_amt < 0:
        if is_lent:
            return True
        if not meta.resolved.is_budget_cadence_window:
            return False
        return is_recurrence_excluded_from_period(
            meta.resolved.effective_period, recurrence
        ) or meta.is_smoothed_annual(cat_lower, meta.resolve_category(cat).lower())
    return meta.resolved.is_budget_cadence_window and (
        not allow_income or cat_lower != "income"
    )


def _is_unfiltered_ledger_excluded(
    tx: Transaction,
    meta: _AnnualCategoryMetadata,
    *,
    include_amortized: bool,
) -> bool:
    """Determine excluded_from_period_budget for an unsliced ledger row when no category/tag filter is set."""
    if not include_amortized:
        return False
    tx_cat = (tx.category or "General").strip() or "General"
    splits = getattr(tx, "splits", None) or ()
    if not splits:
        return _is_ledger_line_excluded(
            tx_cat,
            float(tx.amount),
            tx.recurrence,
            is_lent=_is_lent_loan(getattr(tx, "loan", None)),
            meta=meta,
            allow_income=True,
        )
    non_transfer = [
        s for s in splits if ((s.category or tx_cat).strip().lower() not in TRANSFER_CATEGORIES)
    ]
    return bool(non_transfer) and all(
        _is_ledger_line_excluded(
            (s.category or tx_cat).strip() or "General",
            s.signed_amount(),
            tx.recurrence,
            is_lent=_is_lent_loan(getattr(s, "loan", None)),
            meta=meta,
            allow_income=True,
        )
        for s in non_transfer
    )


def _project_ledger_manual_splits(
    tx: Transaction,
    tx_cat: str,
    parent_tags: tuple[str, ...],
    meta: _AnnualCategoryMetadata,
    *,
    category: Optional[str],
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
    include_amortized: bool,
) -> list[ProjectedTransaction]:
    """Filter and project Manual Split lines in ledger mode (flow=None)."""
    matched: list[tuple[TransactionSplit, str, tuple[str, ...], float]] = []
    for s in tx.splits:
        s_cat = (s.category or tx_cat).strip() or "General"
        s_tags = tuple(normalize_tags([*parent_tags, *parse_rule_tags(s.tags)]))
        if target_tag and target_tag not in s_tags:
            continue
        if not matches_category_subtree(s_cat, target_cat, include_subclasses, meta.lower_to_parent):
            continue
        matched.append((s, s_cat, s_tags, s.signed_amount()))
    if not matched:
        return []
    allow_income = target_cat is None
    can_exclude = include_amortized and (target_cat is None or target_cat not in TRANSFER_CATEGORIES)
    excluded = bool(can_exclude and all(
        _is_ledger_line_excluded(
            s_cat, signed_amt, tx.recurrence,
            is_lent=_is_lent_loan(getattr(s, "loan", None)), meta=meta, allow_income=allow_income,
        )
        for s, s_cat, _, signed_amt in matched
    ))
    return [_build_projected_split_snapshot(
        tx, tx_cat, parent_tags, matched, category=category,
        target_cat=target_cat, include_subclasses=include_subclasses, excluded=excluded,
    )]


def _project_ledger_receipt_lines(
    tx: Transaction,
    tx_cat: str,
    parent_tags: tuple[str, ...],
    meta: _AnnualCategoryMetadata,
    *,
    category: Optional[str],
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
    include_amortized: bool,
) -> list[ProjectedTransaction]:
    """Filter and project Receipt Line slices in ledger mode (flow=None)."""
    scaled, positive_count, tx_nzd = scale_receipt_items(
        tx, tx_cat, parent_tags, exclude_transfers=False
    )
    matched_lines = [
        line for line in scaled
        if (not target_tag or target_tag in line.effective_tags)
        and matches_category_subtree(line.category, target_cat, include_subclasses, meta.lower_to_parent)
    ]
    if not matched_lines:
        return []
    is_lent = _is_lent_loan(getattr(tx, "loan", None))
    allow_income = target_cat is None
    can_exclude = include_amortized and (target_cat is None or target_cat not in TRANSFER_CATEGORIES)
    excluded = bool(can_exclude and all(
        _is_ledger_line_excluded(
            line.category, -line.amount_nzd, tx.recurrence,
            is_lent=is_lent, meta=meta, allow_income=allow_income,
        )
        for line in matched_lines
    ))
    return [_build_receipt_outflow_snapshot(
        tx, tx_cat, parent_tags, tx_nzd, positive_count, matched_lines,
        category=category, target_cat=target_cat,
        include_subclasses=include_subclasses, excluded=excluded,
    )]


def _project_ledger_receipt_or_parent(
    tx: Transaction,
    tx_cat: str,
    parent_tags: tuple[str, ...],
    meta: _AnnualCategoryMetadata,
    *,
    category: Optional[str],
    target_cat: Optional[str],
    target_tag: Optional[str],
    include_subclasses: bool,
    include_amortized: bool,
) -> list[ProjectedTransaction]:
    """Filter and project an unsplit ledger transaction with optional Receipt Line slicing."""
    is_expandable = (
        float(tx.amount) < 0
        and tx.receipt is not None
        and bool(tx.receipt.items)
        and tx_cat.lower() not in TRANSFER_CATEGORIES
    )
    if is_expandable:
        return _project_ledger_receipt_lines(
            tx, tx_cat, parent_tags, meta, category=category, target_cat=target_cat,
            target_tag=target_tag, include_subclasses=include_subclasses,
            include_amortized=include_amortized,
        )
    if target_tag and target_tag not in parent_tags:
        return []
    if not matches_category_subtree(tx_cat, target_cat, include_subclasses, meta.lower_to_parent):
        return []
    can_exclude = include_amortized and (target_cat is None or target_cat not in TRANSFER_CATEGORIES)
    excluded = bool(can_exclude and _is_ledger_line_excluded(
        tx_cat, float(tx.amount), tx.recurrence,
        is_lent=_is_lent_loan(getattr(tx, "loan", None)), meta=meta, allow_income=(target_cat is None),
    ))
    return [_snapshot_transaction(tx, excluded_from_period_budget=excluded)]


def _project_cashflow_mode(
    db: Session,
    query: TransactionQuery,
    meta: _AnnualCategoryMetadata,
    *,
    target_cat: Optional[str],
    target_tag: Optional[str],
) -> TransactionProjection:
    """Execute inflow/outflow cashflow projection mode with exact project_window parity."""
    assert query.flow is not None
    effective_amortized = query.include_amortized if query.flow == "outflow" else False
    rows = _query_window_transactions(
        db, meta.resolved, account_id=query.account_id, loan_id=query.loan_id, include_relations=True
    )
    regular_rows: list[ProjectedTransaction] = []
    for tx in rows:
        regular_rows.extend(_project_cashflow_row(
            tx, meta, flow=query.flow, category=query.category, target_cat=target_cat,
            target_tag=target_tag, include_subclasses=query.include_subclasses,
            include_amortized=effective_amortized,
        ))
    regular_rows.sort(
        key=lambda item: (-item.transaction_date.toordinal(), -item.id, bool(item.excluded_from_period_budget))
    )
    sliced = list(regular_rows[query.offset : query.offset + query.limit])
    if query.flow == "outflow" and effective_amortized and query.offset == 0:
        sliced.extend(_collect_amortized_commitments(
            db, meta, target_cat=target_cat, target_tag=target_tag,
            include_subclasses=query.include_subclasses,
            account_id=query.account_id, loan_id=query.loan_id,
        ))
    return TransactionProjection(items=tuple(sliced), total_count=len(regular_rows))


def _project_unfiltered_ledger_mode(
    db: Session,
    query: TransactionQuery,
    meta: _AnnualCategoryMetadata,
) -> TransactionProjection:
    """Execute paginated unfiltered ledger listing (flow=None, no category or tag filter)."""
    rows, total_count = _query_paginated_window_transactions(
        db, meta.resolved, account_id=query.account_id, loan_id=query.loan_id,
        offset=query.offset, limit=query.limit,
    )
    items = tuple(
        _snapshot_transaction(
            tx,
            excluded_from_period_budget=_is_unfiltered_ledger_excluded(
                tx, meta, include_amortized=query.include_amortized
            ),
        )
        for tx in rows
    )
    return TransactionProjection(items=items, total_count=total_count)


def _project_ledger_filtered_row(
    tx: Transaction,
    query: TransactionQuery,
    meta: _AnnualCategoryMetadata,
    *,
    target_cat: Optional[str],
    target_tag: Optional[str],
) -> list[ProjectedTransaction]:
    """Project one Transaction row in category/tag-filtered ledger mode (flow=None)."""
    tx_cat = (tx.category or "General").strip() or "General"
    parent_tags = tuple(parse_rule_tags(tx.tags))
    projector = (
        _project_ledger_manual_splits
        if getattr(tx, "splits", None)
        else _project_ledger_receipt_or_parent
    )
    return projector(
        tx, tx_cat, parent_tags, meta, category=query.category,
        target_cat=target_cat, target_tag=target_tag,
        include_subclasses=query.include_subclasses,
        include_amortized=query.include_amortized,
    )


def _project_filtered_ledger_mode(
    db: Session,
    query: TransactionQuery,
    meta: _AnnualCategoryMetadata,
    *,
    target_cat: Optional[str],
    target_tag: Optional[str],
) -> TransactionProjection:
    """Execute category/tag-filtered ledger projection (flow=None) preserving Transfers & splits."""
    attach_amortized = bool(
        query.include_amortized and target_cat
        and target_cat not in TRANSFER_CATEGORIES and not target_tag
        and meta.resolved.amortized_recurrences
    )
    amortized_items = (
        _collect_amortized_commitments(
            db, meta, target_cat=target_cat, target_tag=None,
            include_subclasses=query.include_subclasses,
            account_id=query.account_id, loan_id=query.loan_id,
        )
        if attach_amortized else []
    )
    amortized_tx_ids = {item.id for item in amortized_items}
    rows = _query_window_transactions(
        db, meta.resolved, account_id=query.account_id, loan_id=query.loan_id, include_relations=True
    )
    matching: list[ProjectedTransaction] = []
    for tx in rows:
        if tx.id not in amortized_tx_ids:
            matching.extend(_project_ledger_filtered_row(
                tx, query, meta, target_cat=target_cat, target_tag=target_tag
            ))

    max_actual = max(0, query.limit - len(amortized_items)) if attach_amortized else query.limit
    sliced = list(matching[query.offset : query.offset + max_actual]) if max_actual > 0 else []
    if attach_amortized and query.offset == 0:
        sliced.extend(amortized_items[: query.limit])
    return TransactionProjection(items=tuple(sliced), total_count=len(matching))


def project_transactions(
    db: Session,
    query: TransactionQuery,
    *,
    reference_date: date | None = None,
) -> TransactionProjection:
    """Project paginated ledger or inflow/outflow transactions behind a single detached seam."""
    if query.flow is not None and query.flow not in ("inflow", "outflow"):
        raise InvalidWindowError("flow must be 'inflow' or 'outflow'")
    if query.offset < 0:
        raise InvalidWindowError("offset must be non-negative")
    if query.limit < 1:
        raise InvalidWindowError("limit must be positive")

    resolved = resolve_reporting_window(query.window, reference_date=reference_date)
    meta = _load_annual_metadata(db, resolved, rollup_base=query.include_subclasses)
    target_cat = query.category.strip().lower() if query.category and query.category.strip() else None
    target_tag = (
        query.tag.strip().lower().lstrip("#").replace(" ", "-")
        if query.tag and query.tag.strip()
        else None
    )
    if query.flow is not None:
        return _project_cashflow_mode(db, query, meta, target_cat=target_cat, target_tag=target_tag)
    if target_cat is None and target_tag is None:
        return _project_unfiltered_ledger_mode(db, query, meta)
    return _project_filtered_ledger_mode(
        db, query, meta, target_cat=target_cat, target_tag=target_tag
    )


def project_cashflow_transactions(
    db: Session,
    window: ReportingWindow,
    *,
    flow: FlowLiteral,
    category: Optional[str] = None,
    include_subclasses: bool = False,
    include_amortized: bool = False,
    account_id: Optional[int] = None,
    loan_id: Optional[int] = None,
    tag: Optional[str] = None,
    offset: int = 0,
    limit: int = 100,
    reference_date: date | None = None,
) -> TransactionProjection:
    """Compatibility wrapper forwarding inflow/outflow cashflow queries to project_transactions."""
    if flow not in ("inflow", "outflow"):
        raise InvalidWindowError("flow must be 'inflow' or 'outflow'")
    return project_transactions(
        db,
        TransactionQuery(
            window=window,
            category=category,
            tag=tag,
            flow=flow,
            include_subclasses=include_subclasses,
            include_amortized=include_amortized,
            account_id=account_id,
            loan_id=loan_id,
            limit=limit,
            offset=offset,
        ),
        reference_date=reference_date,
    )
