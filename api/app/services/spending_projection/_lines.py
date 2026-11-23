"""Expense line expansion, subtree matching, recurring allocations, annual context, and snapshot mappers."""

from datetime import date, timedelta
from typing import Optional, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.models import BudgetRule, Category, Receipt, Transaction, TransactionSplit
from app.services.category_taxonomy import (
    get_category_parent_map,
    get_descendant_category_names,
    get_parent_category,
    get_root_category,
)
from app.services.fx_service import to_nzd_amount
from app.services.rules_engine import normalize_tags, parse_rule_tags
from app.services.spending_projection._types import (
    DAYS_PER_YEAR,
    RECURRENCE_LOOKBACK_DAYS,
    RECURRENCE_WEEKLY_FACTOR,
    TRANSFER_CATEGORIES,
    ExpenseLine,
    ProjectedSplit,
    ProjectedTransaction,
    RecurringCommitmentAllocation,
    ResolvedWindow,
    _AnnualCategoryMetadata,
    _BudgetProjectionContext,
)
from app.services.spending_projection._window import _transaction_eager_options, weekly_to_monthly


def _is_lent_loan(loan_obj: object) -> bool:
    """Return True when an attached Loan ORM row has loan_type == 'lent'."""
    return (
        loan_obj is not None
        and (getattr(loan_obj, "loan_type", None) or "").strip().lower() == "lent"
    )


def matches_category_subtree(
    cat: str,
    target_cat: Optional[str],
    include_subclasses: bool,
    lower_to_parent: dict[str, str],
) -> bool:
    """Return True when cat matches target_cat directly or anywhere within its descendant subtree."""
    if not target_cat:
        return True
    curr = (cat or "General").strip().lower() or "general"
    if not include_subclasses:
        return curr == target_cat
    seen: set[str] = set()
    while curr and curr not in seen:
        if curr == target_cat:
            return True
        seen.add(curr)
        curr = lower_to_parent.get(curr, "")
    return False


def _expand_manual_split_lines(
    tx: Transaction,
    tx_cat: str,
    parent_tags: list[str],
) -> list[ExpenseLine]:
    """Expand manual TransactionSplit rows into qualifying NZD ExpenseLine items."""
    lines: list[ExpenseLine] = []
    for s in getattr(tx, "splits", None) or []:
        signed_amt = s.signed_amount()
        if signed_amt >= 0 or _is_lent_loan(getattr(s, "loan", None)):
            continue
        raw_cat = (s.category or tx_cat).strip() or tx_cat
        if raw_cat.lower() in TRANSFER_CATEGORIES:
            continue
        line_nzd = round(to_nzd_amount(abs(signed_amt), tx.currency, tx.exchange_rate), 2)
        line_tags = tuple(normalize_tags([*parent_tags, *parse_rule_tags(s.tags)]))
        lines.append(
            ExpenseLine(
                category=raw_cat,
                amount_nzd=line_nzd,
                effective_tags=line_tags,
                item_name=s.notes,
                source="manual_split",
            )
        )
    return lines


def scale_receipt_items(
    tx: Transaction,
    tx_cat: str,
    parent_tags: Sequence[str],
    *,
    exclude_transfers: bool = True,
) -> tuple[list[ExpenseLine], int, float]:
    """Scale positive receipt items pro-rata to NZD, returning (lines, positive_item_count, tx_nzd)."""
    tx_nzd = round(to_nzd_amount(abs(float(tx.amount)), tx.currency, tx.exchange_rate), 2)
    receipt = getattr(tx, "receipt", None)
    items = getattr(receipt, "items", None) if receipt is not None else None
    positive_items = [it for it in (items or []) if float(it.total_price) > 0]
    items_sum = sum(float(it.total_price) for it in positive_items)
    if items_sum <= 0 or not positive_items:
        return [], 0, tx_nzd

    scale = tx_nzd / items_sum
    scaled = [
        ExpenseLine(
            category=(it.category or tx_cat).strip() or tx_cat,
            amount_nzd=round(float(it.total_price) * scale, 2),
            effective_tags=tuple(normalize_tags([*parent_tags, *parse_rule_tags(it.tags)])),
            item_name=it.item_name,
            source="receipt_item",
        )
        for it in positive_items
    ]
    _apply_rounding_delta(scaled, tx_nzd)
    if not exclude_transfers:
        return scaled, len(positive_items), tx_nzd
    non_transfer = [line for line in scaled if line.category.lower() not in TRANSFER_CATEGORIES]
    return non_transfer, len(positive_items), tx_nzd


def _apply_rounding_delta(scaled: list[ExpenseLine], tx_nzd: float) -> None:
    """Absorb pro-rata rounding drift onto the largest scaled receipt ExpenseLine."""
    delta = round(tx_nzd - sum(line.amount_nzd for line in scaled), 2)
    if delta == 0.0 or not scaled:
        return
    max_idx = max(range(len(scaled)), key=lambda i: scaled[i].amount_nzd)
    target = scaled[max_idx]
    scaled[max_idx] = ExpenseLine(
        category=target.category,
        amount_nzd=round(target.amount_nzd + delta, 2),
        effective_tags=target.effective_tags,
        item_name=target.item_name,
        source=target.source,
    )


def _expand_receipt_or_parent_lines(
    tx: Transaction,
    tx_cat: str,
    parent_tags: list[str],
) -> list[ExpenseLine]:
    """Expand an unsplit transaction via receipt line items or parent category in NZD."""
    if float(tx.amount) >= 0 or _is_lent_loan(getattr(tx, "loan", None)):
        return []
    if tx_cat.lower() in TRANSFER_CATEGORIES:
        return []
    scaled, positive_count, tx_nzd = scale_receipt_items(
        tx, tx_cat, parent_tags, exclude_transfers=True
    )
    if positive_count > 0:
        return scaled
    return [
        ExpenseLine(
            category=tx_cat,
            amount_nzd=tx_nzd,
            effective_tags=tuple(parent_tags),
            source="parent",
        )
    ]


def expand_expense_lines(tx: Transaction) -> list[ExpenseLine]:
    """Expand a transaction into canonical NZD ExpenseLine items (splits -> receipt -> parent)."""
    tx_cat = (tx.category or "General").strip() or "General"
    parent_tags = parse_rule_tags(tx.tags)
    if getattr(tx, "splits", None):
        return _expand_manual_split_lines(tx, tx_cat, parent_tags)
    return _expand_receipt_or_parent_lines(tx, tx_cat, parent_tags)


def expand_expense_buckets(tx: Transaction) -> list[tuple[str, float]]:
    """Collapse a transaction's ExpenseLine items into per-category (category, amount_nzd) buckets."""
    grouped: dict[str, float] = {}
    canonical: dict[str, str] = {}
    for line in expand_expense_lines(tx):
        key = line.category.lower()
        canonical.setdefault(key, line.category)
        grouped[key] = grouped.get(key, 0.0) + line.amount_nzd
    return [(canonical[key], round(amt, 2)) for key, amt in grouped.items()]


def _resolve_unsplit_inflow_nzd(tx: Transaction, amt_nzd: float, *, is_lent: bool) -> float:
    """Compute net income NZD for a positive unsplit transaction, excluding lent principal repayment."""
    if not is_lent:
        return amt_nzd
    if tx.loan_payment_amount is None:
        return 0.0
    tx_currency = (tx.currency or "NZD").strip().upper()
    repayment_nzd = (
        to_nzd_amount(float(tx.loan_payment_amount), tx.currency, tx.exchange_rate)
        if tx_currency != "NZD"
        else float(tx.loan_payment_amount)
    )
    return max(0.0, amt_nzd - repayment_nzd)


def _nzd_to_tx_currency(amount_nzd: float, tx: Transaction) -> float:
    """Convert a positive NZD amount back to the parent transaction's native currency."""
    fx_unit = to_nzd_amount(1.0, tx.currency, tx.exchange_rate)
    return round(amount_nzd / fx_unit, 2) if fx_unit > 0 else round(amount_nzd, 2)


def _extract_recurring_outflow_slices(tx: Transaction) -> list[tuple[str, float]]:
    """Extract qualifying (category, positive_original_amount) slices for a recurring transaction."""
    tx_cat = (tx.category or "General").strip() or "General"
    splits = getattr(tx, "splits", None) or []
    if splits:
        grouped_orig: dict[str, float] = {}
        canonical_cat: dict[str, str] = {}
        for s in splits:
            signed_amt = s.signed_amount()
            if signed_amt >= 0 or _is_lent_loan(getattr(s, "loan", None)):
                continue
            s_cat = (s.category or tx_cat).strip() or tx_cat
            s_lower = s_cat.lower()
            if s_lower in TRANSFER_CATEGORIES:
                continue
            canonical_cat.setdefault(s_lower, s_cat)
            grouped_orig[s_lower] = grouped_orig.get(s_lower, 0.0) + abs(signed_amt)
        return [(canonical_cat[k], round(amt, 2)) for k, amt in grouped_orig.items()]

    if float(tx.amount) >= 0 or _is_lent_loan(getattr(tx, "loan", None)):
        return []
    if tx_cat.lower() in TRANSFER_CATEGORIES:
        return []
    return [(tx_cat, round(abs(float(tx.amount)), 2))]


def _build_tx_commitment_allocations(
    tx: Transaction,
    rec: str,
    factor: float,
    tx_slices: list[tuple[str, float]],
) -> list[RecurringCommitmentAllocation]:
    """Build RecurringCommitmentAllocation entries for each category slice of a recurring tx."""
    merchant_name = (tx.merchant or "").strip() or "Unnamed commitment"
    currency_code = (tx.currency or "NZD").strip().upper()
    result: list[RecurringCommitmentAllocation] = []
    for cat_name, orig_amt in tx_slices:
        amt_nzd = to_nzd_amount(orig_amt, tx.currency, tx.exchange_rate)
        wk_nzd = round(amt_nzd * factor, 2)
        result.append(
            RecurringCommitmentAllocation(
                transaction_id=tx.id,
                merchant=merchant_name,
                category=cat_name,
                recurrence=rec,
                last_charged_date=tx.transaction_date,
                original_amount=round(orig_amt, 2),
                currency=currency_code,
                amount_nzd=round(amt_nzd, 2),
                weekly_nzd=wk_nzd,
                monthly_nzd=weekly_to_monthly(wk_nzd),
                account_id=tx.account_id,
            )
        )
    return result


def _query_recurring_candidate_transactions(
    db: Session,
    as_of: date,
    *,
    include_relations: bool = False,
) -> list[Transaction]:
    """Fetch candidate recurring outflow transactions within the maximum 366-day lookback."""
    max_lookback = as_of - timedelta(days=366)
    stmt = (
        select(Transaction)
        .options(
            *_transaction_eager_options(
                include_receipt_items=False,
                include_relations=include_relations,
            )
        )
        .where(
            Transaction.transaction_date >= max_lookback,
            Transaction.transaction_date <= as_of,
            or_(
                Transaction.amount < 0,
                Transaction.splits.any(TransactionSplit.amount < 0),
            ),
            Transaction.recurrence.in_(tuple(RECURRENCE_WEEKLY_FACTOR.keys())),
            or_(
                func.lower(Transaction.category).not_in(TRANSFER_CATEGORIES),
                Transaction.splits.any(),
            ),
        )
        .order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
    )
    return list(db.scalars(stmt).unique().all())


def _iter_active_recurring_allocations(
    db: Session,
    as_of: date,
    *,
    include_relations: bool = False,
) -> list[tuple[Transaction, RecurringCommitmentAllocation, bool]]:
    """Return (source_tx, allocation, is_split_slice) for active recurring commitments."""
    rows = _query_recurring_candidate_transactions(
        db, as_of, include_relations=include_relations
    )
    seen: set[tuple[str, str]] = set()
    entries: list[tuple[Transaction, RecurringCommitmentAllocation, bool]] = []
    for tx in rows:
        rec = (tx.recurrence or "").strip().lower()
        factor = RECURRENCE_WEEKLY_FACTOR.get(rec)
        if not factor or (as_of - tx.transaction_date).days > RECURRENCE_LOOKBACK_DAYS[rec]:
            continue
        tx_slices = _extract_recurring_outflow_slices(tx)
        if not tx_slices:
            continue
        key = ((tx.merchant or "").strip().lower(), rec)
        if key in seen:
            continue
        seen.add(key)
        is_split_slice = bool(getattr(tx, "splits", None))
        for alloc in _build_tx_commitment_allocations(tx, rec, factor, tx_slices):
            entries.append((tx, alloc, is_split_slice))
    return entries


def get_active_recurring_commitment_allocations(
    db: Session,
    as_of: date,
) -> list[RecurringCommitmentAllocation]:
    """Return per-category active recurring commitment allocations within lookback windows."""
    return [alloc for _, alloc, _ in _iter_active_recurring_allocations(db, as_of)]


def _query_annual_candidate_txs(db: Session, start_365: date, as_of: date) -> list[Transaction]:
    """Fetch 365-day candidate outflow transactions for Annualized Category aggregation."""
    stmt = (
        select(Transaction)
        .options(
            joinedload(Transaction.receipt).joinedload(Receipt.items),
            joinedload(Transaction.loan),
            joinedload(Transaction.splits).joinedload(TransactionSplit.loan),
        )
        .where(
            Transaction.transaction_date >= start_365,
            Transaction.transaction_date <= as_of,
            or_(Transaction.amount < 0, Transaction.splits.any(TransactionSplit.amount < 0)),
            or_(func.lower(Transaction.category).not_in(TRANSFER_CATEGORIES), Transaction.splits.any()),
        )
    )
    return list(db.scalars(stmt).unique().all())


def _compute_annual_category_totals(
    db: Session,
    as_of: date,
    categories: set[str],
) -> tuple[dict[str, float], dict[str, int]]:
    """Single-pass 365-day query returning (spend_map, count_map) for Annualized Categories."""
    target_lower = {c.strip().lower() for c in categories if c and c.strip()}
    if not target_lower:
        return {}, {}
    txs = _query_annual_candidate_txs(db, as_of - timedelta(days=DAYS_PER_YEAR), as_of)
    spend_map: dict[str, float] = {}
    count_map: dict[str, int] = {}
    for tx in txs:
        for cat_name, amt_nzd in expand_expense_buckets(tx):
            key = cat_name.lower()
            if key in target_lower:
                spend_map[key] = spend_map.get(key, 0.0) + amt_nzd
                count_map[key] = count_map.get(key, 0) + 1
    return spend_map, count_map


def _build_canonical_name_map(
    raw_budget_rules: dict[str, BudgetRule],
    parent_map: dict[str, str],
) -> dict[str, str]:
    """Build lowercase-to-canonical display category map from rules and taxonomy."""
    names: dict[str, str] = {
        str(r.category).strip().lower(): str(r.category).strip()
        for r in raw_budget_rules.values()
        if r.category and str(r.category).strip()
    }
    for child_name, parent_name in parent_map.items():
        names.setdefault(child_name.lower(), child_name)
        names.setdefault(parent_name.lower(), parent_name)
    return names


def _apply_category_annual_flags(
    db: Session,
    annual_cats: set[str],
    canonical_name_map: dict[str, str],
) -> None:
    """Populate canonical category names and annual amortization flags from Category rows."""
    for db_cat in db.scalars(select(Category)).all():
        cat_name = (db_cat.name or "").strip()
        if not cat_name:
            continue
        cat_lower = cat_name.lower()
        canonical_name_map.setdefault(cat_lower, cat_name)
        if db_cat.is_active and (db_cat.amortize_period or "").strip().lower() == "annual":
            annual_cats.add(cat_lower)


def _load_annual_metadata(
    db: Session,
    resolved: ResolvedWindow,
    *,
    rollup_base: bool,
) -> _AnnualCategoryMetadata:
    """Load category taxonomy, active budget rules, and Annualized Category sets."""
    parent_map = get_category_parent_map(db)
    lower_to_parent = {
        k.strip().lower(): v.strip().lower()
        for k, v in parent_map.items()
        if k and k.strip() and v and v.strip()
    }
    active_rules = list(db.scalars(select(BudgetRule).where(BudgetRule.is_active == True)).all())
    raw_budget_rules: dict[str, BudgetRule] = {
        str(r.category).lower(): r for r in active_rules if r.category
    }
    annual_cats: set[str] = {
        cat_lower
        for cat_lower, rule in raw_budget_rules.items()
        if (rule.budget_period or "monthly").strip().lower() == "annual"
    }
    canonical_name_map = _build_canonical_name_map(raw_budget_rules, parent_map)
    _apply_category_annual_flags(db, annual_cats, canonical_name_map)
    annual_base_roots = {
        c for c in annual_cats
        if get_parent_category(canonical_name_map.get(c) or c, parent_map) is None
    }
    return _AnnualCategoryMetadata(
        resolved=resolved, rollup_base=rollup_base, parent_map=parent_map,
        lower_to_parent=lower_to_parent, canonical_name_map=canonical_name_map,
        raw_budget_rules=raw_budget_rules, annual_cats=frozenset(annual_cats),
        annual_base_roots=frozenset(annual_base_roots),
    )


def _rollup_budget_rules_to_base(
    raw_budget_rules: dict[str, BudgetRule],
    parent_map: dict[str, str],
    canonical_name_map: dict[str, str],
    annual_base_roots: frozenset[str],
) -> dict[str, BudgetRule]:
    """Aggregate child and root BudgetRules under each base class for group_by=base."""
    grouped_rules: dict[str, list[BudgetRule]] = {}
    grouped_names: dict[str, str] = {}
    for rule in raw_budget_rules.values():
        root_name = get_root_category(rule.category, parent_map)
        root_lower = root_name.lower()
        grouped_names[root_lower] = canonical_name_map.get(root_lower) or root_name
        grouped_rules.setdefault(root_lower, []).append(rule)

    rolled: dict[str, BudgetRule] = {}
    for root_lower, rules_list in grouped_rules.items():
        root_rule = next(
            (r for r in rules_list if r.category.strip().lower() == root_lower),
            None,
        )
        if root_lower in annual_base_roots:
            resolved_period = "annual"
        elif root_rule is not None:
            resolved_period = (root_rule.budget_period or "monthly").strip().lower()
        else:
            child_periods = {(r.budget_period or "monthly").strip().lower() for r in rules_list}
            resolved_period = next(iter(child_periods)) if len(child_periods) == 1 else "monthly"
        rolled[root_lower] = BudgetRule(
            category=grouped_names[root_lower],
            weekly_limit=round(sum(float(r.weekly_limit) for r in rules_list), 2),
            monthly_limit=round(sum(float(r.monthly_limit) for r in rules_list), 2),
            budget_period=resolved_period,
            is_active=True,
        )
    return rolled


def _rollup_annual_maps_to_base(
    raw_spend: dict[str, float],
    raw_count: dict[str, int],
    meta: _AnnualCategoryMetadata,
) -> tuple[dict[str, float], dict[str, int]]:
    """Roll up raw 365-day annual spend and count maps into Root Category buckets."""
    annual_spend_map: dict[str, float] = {}
    annual_count_map: dict[str, int] = {}
    for raw_cat_lower, spend_val in raw_spend.items():
        display = meta.canonical_name_map.get(raw_cat_lower) or raw_cat_lower
        root_lower = get_root_category(display, meta.parent_map).lower()
        target_key = root_lower if root_lower in meta.annual_base_roots else raw_cat_lower
        annual_spend_map[target_key] = annual_spend_map.get(target_key, 0.0) + spend_val
    for raw_cat_lower, cnt_val in raw_count.items():
        display = meta.canonical_name_map.get(raw_cat_lower) or raw_cat_lower
        root_lower = get_root_category(display, meta.parent_map).lower()
        target_key = root_lower if root_lower in meta.annual_base_roots else raw_cat_lower
        annual_count_map[target_key] = annual_count_map.get(target_key, 0) + cnt_val
    return annual_spend_map, annual_count_map


def _load_budget_context(
    db: Session,
    resolved: ResolvedWindow,
    *,
    rollup_base: bool,
) -> _BudgetProjectionContext:
    """Load category taxonomy, active budget rules, and 365-day annual category totals."""
    meta = _load_annual_metadata(db, resolved, rollup_base=rollup_base)
    fetch_annual_cats = set(meta.annual_cats)
    if rollup_base:
        for root_lower in meta.annual_base_roots:
            fetch_annual_cats.update(get_descendant_category_names(root_lower, meta.parent_map))
    raw_spend, raw_count = (
        _compute_annual_category_totals(db, resolved.as_of_date, fetch_annual_cats)
        if resolved.is_budget_cadence_window
        else ({}, {})
    )
    if not rollup_base:
        return _BudgetProjectionContext(
            resolved=resolved, rollup_base=False, parent_map=meta.parent_map,
            canonical_name_map=meta.canonical_name_map, annual_cats=meta.annual_cats,
            annual_base_roots=meta.annual_base_roots, effective_annual_set=meta.annual_cats,
            effective_budget_rules=meta.raw_budget_rules,
            annual_spend_map=raw_spend, annual_count_map=raw_count,
        )
    annual_spend_map, annual_count_map = _rollup_annual_maps_to_base(raw_spend, raw_count, meta)
    rolled_rules = _rollup_budget_rules_to_base(
        meta.raw_budget_rules, meta.parent_map, meta.canonical_name_map, meta.annual_base_roots
    )
    return _BudgetProjectionContext(
        resolved=resolved, rollup_base=True, parent_map=meta.parent_map,
        canonical_name_map=meta.canonical_name_map, annual_cats=meta.annual_cats,
        annual_base_roots=meta.annual_base_roots, effective_annual_set=meta.annual_base_roots,
        effective_budget_rules=rolled_rules,
        annual_spend_map=annual_spend_map, annual_count_map=annual_count_map,
    )


def _snapshot_split(split: TransactionSplit) -> ProjectedSplit:
    """Capture an immutable detached-session-safe ProjectedSplit from an ORM split row."""
    return ProjectedSplit(
        id=split.id,
        transaction_id=split.transaction_id,
        amount=float(split.amount),
        category=split.category,
        tags=tuple(parse_rule_tags(split.tags)),
        loan_id=split.loan_id,
        loan_name=split.loan.name if split.loan else None,
        loan_type=split.loan.loan_type if split.loan else None,
        notes=split.notes,
    )


def _extract_linked_fields(tx: Transaction) -> tuple[str | None, float | None, str | None]:
    """Extract linked transfer account name, amount, and currency without leaking ORM objects."""
    linked = tx.linked_transaction
    if linked is None:
        return None, None, None
    acct_name = linked.account.name if linked.account else None
    return acct_name, float(linked.amount), linked.currency


def _snapshot_transaction(
    tx: Transaction, *, amount_override: float | None = None,
    category_override: str | None = None, tags_override: tuple[str, ...] | None = None,
    notes_override: str | None = None, is_split_override: bool | None = None,
    is_receipt_split: bool = False, excluded_from_period_budget: bool = False,
    is_amortized_commitment: bool = False, amortized_period_amount_nzd: float | None = None,
    amortized_cadence: str | None = None,
) -> ProjectedTransaction:
    """Capture a detached-session-safe ProjectedTransaction with optional projection overrides."""
    splits_tuple = tuple(_snapshot_split(s) for s in (getattr(tx, "splits", None) or ()))
    linked_acct, linked_amt, linked_ccy = _extract_linked_fields(tx)
    receipt = tx.receipt
    return ProjectedTransaction(
        id=tx.id, transaction_date=tx.transaction_date, account_id=tx.account_id,
        account_name=tx.account.name if tx.account else None,
        amount=float(amount_override if amount_override is not None else tx.amount),
        currency=tx.currency, merchant=tx.merchant,
        category=category_override if category_override is not None else tx.category,
        raw_description=tx.raw_description,
        notes=notes_override if notes_override is not None else tx.notes,
        linked_transaction_id=tx.linked_transaction_id, linked_account_name=linked_acct,
        linked_amount=linked_amt, linked_currency=linked_ccy,
        counterpart_account_id=tx.counterpart_account_id,
        counterpart_account_name=tx.counterpart_account.name if tx.counterpart_account else None,
        counterpart_account_type=tx.counterpart_account.account_type if tx.counterpart_account else None,
        loan_id=tx.loan_id, loan_name=tx.loan.name if tx.loan else None,
        loan_type=tx.loan.loan_type if tx.loan else None,
        loan_payment_amount=float(tx.loan_payment_amount) if tx.loan_payment_amount is not None else None,
        exchange_rate=float(tx.exchange_rate) if tx.exchange_rate is not None else None,
        fee_amount=float(tx.fee_amount) if tx.fee_amount is not None else None, raw_hash=tx.raw_hash,
        tags=tags_override if tags_override is not None else tuple(parse_rule_tags(tx.tags)),
        recurrence=tx.recurrence, has_receipt=bool(receipt), receipt_id=receipt.id if receipt else None,
        receipt_document_type=receipt.document_type if receipt else None,
        is_receipt_split=is_receipt_split, splits=splits_tuple,
        is_split=is_split_override if is_split_override is not None else (len(splits_tuple) >= 2),
        is_amortized_commitment=is_amortized_commitment,
        amortized_period_amount_nzd=amortized_period_amount_nzd, amortized_cadence=amortized_cadence,
        excluded_from_period_budget=excluded_from_period_budget, created_at=tx.created_at,
    )
