"""Three-tier receipt/payslip transaction matching and email semantic deduplication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import re
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models import Receipt, ReceiptItem, Transaction
from app.services.fx_service import get_exchange_rate
from app.services.receipt_ingestion._types import (
    ReceiptItemSnapshot,
    ReceiptMatchOutcome,
    ReceiptNotFoundError,
    ReceiptSnapshot,
    SemanticDuplicateMatch,
    TransactionCandidateSnapshot,
)
from app.services.rules_engine import parse_rule_tags
from app.services.receipt_ingestion.sanitization import (
    _MERCHANT_STOPWORDS,
    normalize_merchant_key,
)

_ORIG_FX_DESC_RE = re.compile(
    r"\bof\s+([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s+([A-Z]{3})\b",
    re.IGNORECASE,
)
_BANK_FX_DESC_RE = re.compile(
    r"(?:^|[\s(])([A-Z]{3})\s+([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:@|converted\s+at\b)",
    re.IGNORECASE,
)
_FX_DESC_PATTERNS: tuple[tuple[re.Pattern[str], bool], ...] = (
    (_ORIG_FX_DESC_RE, False),
    (_BANK_FX_DESC_RE, True),
)
_MONTH_CODES = frozenset(
    {"JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"}
)


@dataclass(frozen=True)
class _ScoredCandidate:
    tx: Transaction
    tier_rank: int
    match_reason: str
    confidence: float
    has_merchant_overlap: bool
    sim_score: float
    date_diff: int
    amt_diff: float
    original_amount: Optional[float] = None
    original_currency: Optional[str] = None
    estimated_receipt_amount: Optional[float] = None


def parse_original_fx_from_description(
    raw_description: Optional[str],
) -> Optional[tuple[float, str]]:
    """Extract original foreign currency amount and ISO code from bank/Wise raw_description."""
    if not raw_description:
        return None
    for pattern, code_first in _FX_DESC_PATTERNS:
        for match in pattern.finditer(raw_description):
            code, amt_str = match.groups() if code_first else (match.group(2), match.group(1))
            upper_code = code.upper()
            if upper_code in _MONTH_CODES:
                continue
            try:
                amt_val = float(amt_str.replace(",", ""))
            except ValueError:
                continue
            if amt_val > 0:
                return amt_val, upper_code
    return None


def _merchant_tokens(text: str) -> set[str]:
    return {
        tok
        for tok in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(tok) >= 2 and tok not in _MERCHANT_STOPWORDS
    }


def _merchant_similarity(
    receipt_merchant: str,
    tx_merchant: str,
    tx_raw_desc: Optional[str],
) -> tuple[bool, float]:
    r_tokens = _merchant_tokens(receipt_merchant)
    tx_tokens = _merchant_tokens(tx_merchant) | _merchant_tokens(tx_raw_desc or "")

    r_compact = re.sub(r"[^a-z0-9]", "", (receipt_merchant or "").lower())
    tx_m_compact = re.sub(r"[^a-z0-9]", "", (tx_merchant or "").lower())
    tx_d_compact = re.sub(r"[^a-z0-9]", "", (tx_raw_desc or "").lower())

    substring_hit = False
    if len(r_compact) >= 4:
        substring_hit = (
            r_compact in tx_m_compact
            or r_compact in tx_d_compact
            or (len(tx_m_compact) >= 4 and tx_m_compact in r_compact)
        )

    overlap = len(r_tokens & tx_tokens)
    has_overlap = overlap > 0 or substring_hit
    if not r_tokens and not substring_hit:
        return False, 0.0
    union_size = len(r_tokens | tx_tokens) or 1
    jaccard = overlap / union_size
    score = max(jaccard, 0.6 if substring_hit else 0.0)
    return has_overlap, round(score, 4)


def _resolve_tx_to_receipt_fx_rate(
    db: Session,
    tx: Transaction,
    receipt_currency: str,
) -> Optional[float]:
    tx_curr = (tx.currency or "NZD").strip().upper()
    rec_curr = (receipt_currency or "NZD").strip().upper()
    if tx_curr == rec_curr:
        return 1.0
    if rec_curr == "NZD" and tx.exchange_rate is not None and float(tx.exchange_rate) > 0:
        return float(tx.exchange_rate)
    fx_res = get_exchange_rate(
        db,
        rate_date=tx.transaction_date,
        from_currency=tx_curr,
        to_currency=rec_curr,
    )
    if fx_res is not None and fx_res[0] > 0:
        return float(fx_res[0])
    return None


def _resolve_candidate_window(receipt: Receipt, is_payslip: bool) -> tuple[date, date]:
    r_date = receipt.receipt_date
    if is_payslip:
        return r_date - timedelta(days=7), r_date + timedelta(days=14)
    if receipt.order_number or receipt.source_email_message_id:
        return r_date - timedelta(days=3), r_date + timedelta(days=21)
    return r_date - timedelta(days=1), r_date + timedelta(days=7)


def _score_fx_estimated_candidate(
    db: Session,
    tx: Transaction,
    *,
    tx_amt: float,
    target_amt: float,
    rec_curr: str,
    has_overlap: bool,
    sim_score: float,
    date_diff: int,
    orig_amt: Optional[float],
    orig_curr: Optional[str],
) -> Optional[_ScoredCandidate]:
    rate = _resolve_tx_to_receipt_fx_rate(db, tx, rec_curr)
    if rate is None or rate <= 0 or target_amt <= 0:
        return None
    est_amt = round(tx_amt * rate, 2)
    rel_err = abs(est_amt - target_amt) / target_amt
    if rel_err > 0.05:
        return None
    base_conf = 0.85 if has_overlap else 0.68
    conf = round(
        max(0.40, min(0.89, base_conf - rel_err * 3.0 - 0.005 * min(date_diff, 14))), 2
    )
    return _ScoredCandidate(
        tx=tx,
        tier_rank=1,
        match_reason="fx_estimated",
        confidence=conf,
        has_merchant_overlap=has_overlap,
        sim_score=sim_score,
        date_diff=date_diff,
        amt_diff=abs(est_amt - target_amt),
        original_amount=orig_amt,
        original_currency=orig_curr,
        estimated_receipt_amount=est_amt,
    )


def _score_candidate_transaction(
    db: Session,
    tx: Transaction,
    *,
    target_amt: float,
    r_date: date,
    rec_curr: str,
    receipt_merchant: str,
) -> Optional[_ScoredCandidate]:
    tx_curr = (tx.currency or "NZD").strip().upper()
    tx_amt = abs(float(tx.amount))
    date_diff = abs((tx.transaction_date - r_date).days)
    has_overlap, sim_score = _merchant_similarity(
        receipt_merchant, tx.merchant, tx.raw_description
    )
    orig_amt, orig_curr = parse_original_fx_from_description(tx.raw_description) or (
        None,
        None,
    )
    if tx_curr == rec_curr and abs(tx_amt - target_amt) <= 0.05:
        amt_diff = abs(tx_amt - target_amt)
        base = 0.96 if has_overlap else 0.78
        conf = round(
            max(0.50, min(0.99, base - 0.01 * min(date_diff, 14) - 0.5 * amt_diff)), 2
        )
        return _ScoredCandidate(
            tx=tx,
            tier_rank=3,
            match_reason="exact_same_currency",
            confidence=conf,
            has_merchant_overlap=has_overlap,
            sim_score=sim_score,
            date_diff=date_diff,
            amt_diff=amt_diff,
            original_amount=orig_amt,
            original_currency=orig_curr,
            estimated_receipt_amount=round(tx_amt, 2),
        )
    if orig_curr == rec_curr and orig_amt is not None and abs(orig_amt - target_amt) <= 0.05:
        amt_diff = abs(orig_amt - target_amt)
        base = 0.93 if has_overlap else 0.65
        conf = round(
            max(0.45, min(0.98, base - 0.008 * min(date_diff, 14) - 0.5 * amt_diff)), 2
        )
        return _ScoredCandidate(
            tx=tx,
            tier_rank=2,
            match_reason="original_fx_description",
            confidence=conf,
            has_merchant_overlap=has_overlap,
            sim_score=sim_score,
            date_diff=date_diff,
            amt_diff=amt_diff,
            original_amount=orig_amt,
            original_currency=orig_curr,
            estimated_receipt_amount=round(orig_amt, 2),
        )
    if tx_curr == rec_curr:
        return None
    return _score_fx_estimated_candidate(
        db,
        tx,
        tx_amt=tx_amt,
        target_amt=target_amt,
        rec_curr=rec_curr,
        has_overlap=has_overlap,
        sim_score=sim_score,
        date_diff=date_diff,
        orig_amt=orig_amt,
        orig_curr=orig_curr,
    )


def _evaluate_receipt_candidates(
    db: Session,
    receipt: Receipt,
) -> list[_ScoredCandidate]:
    target_amt = float(receipt.total_amount)
    r_date = receipt.receipt_date
    rec_curr = (receipt.currency or "NZD").strip().upper()
    is_payslip = getattr(receipt, "document_type", "receipt") == "payslip"
    date_min, date_max = _resolve_candidate_window(receipt, is_payslip)
    sign_filter = Transaction.amount > 0 if is_payslip else Transaction.amount < 0

    linked_tx_subq = select(Receipt.transaction_id).where(
        Receipt.transaction_id.isnot(None), Receipt.id != receipt.id
    )
    stmt = (
        select(Transaction)
        .options(joinedload(Transaction.account))
        .where(sign_filter, Transaction.transaction_date >= date_min)
        .where(Transaction.transaction_date <= date_max, ~Transaction.id.in_(linked_tx_subq))
    )
    candidates = [
        scored
        for tx in db.scalars(stmt).all()
        if (
            scored := _score_candidate_transaction(
                db,
                tx,
                target_amt=target_amt,
                r_date=r_date,
                rec_curr=rec_curr,
                receipt_merchant=receipt.merchant,
            )
        )
        is not None
    ]
    candidates.sort(
        key=lambda c: (
            c.tier_rank,
            1 if c.has_merchant_overlap else 0,
            c.confidence,
            -c.date_diff,
            -c.amt_diff,
        ),
        reverse=True,
    )
    return candidates


def _to_candidate_snapshot(item: _ScoredCandidate) -> TransactionCandidateSnapshot:
    tx = item.tx
    return TransactionCandidateSnapshot(
        transaction_id=tx.id,
        transaction_date=tx.transaction_date,
        merchant=tx.merchant,
        raw_description=tx.raw_description,
        amount=float(tx.amount),
        currency=tx.currency,
        account_id=tx.account_id,
        account_name=tx.account.name if tx.account else None,
        original_amount=item.original_amount,
        original_currency=item.original_currency,
        estimated_receipt_amount=item.estimated_receipt_amount,
        confidence=item.confidence,
        match_reason=item.match_reason,
    )


def find_receipt_transaction_candidates(
    db: Session,
    receipt_id: int,
    *,
    limit: int = 5,
) -> tuple[TransactionCandidateSnapshot, ...]:
    """Return top sorted detached candidate matching transactions for a receipt."""
    receipt = db.get(Receipt, receipt_id)
    if receipt is None:
        raise ReceiptNotFoundError(f"Receipt {receipt_id} not found")
    if limit <= 0:
        return ()
    scored = _evaluate_receipt_candidates(db, receipt)
    return tuple(_to_candidate_snapshot(item) for item in scored[:limit])


def _select_unambiguous_winner(
    candidates: list[_ScoredCandidate],
) -> Optional[_ScoredCandidate]:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    def rank_key(c: _ScoredCandidate) -> tuple[int, float, int, float]:
        return (
            1 if c.has_merchant_overlap else 0,
            round(c.sim_score, 4),
            -c.date_diff,
            -round(c.amt_diff, 4),
        )

    ranked = sorted(candidates, key=rank_key, reverse=True)
    if rank_key(ranked[0]) > rank_key(ranked[1]):
        return ranked[0]
    return None


def match_receipt_to_transaction(
    db: Session,
    receipt_id: int,
) -> ReceiptMatchOutcome:
    """Match a receipt unambiguously via 3-tier scoring, else flag needs_review or unlinked."""
    receipt = db.get(Receipt, receipt_id)
    if receipt is None:
        raise ReceiptNotFoundError(f"Receipt {receipt_id} not found")

    if receipt.transaction_id is not None:
        return ReceiptMatchOutcome(
            receipt_id=receipt.id,
            matched_transaction_id=receipt.transaction_id,
            status=receipt.status,
            match_reason=None,
            confidence=None,
            candidate_count=0,
        )

    candidates = _evaluate_receipt_candidates(db, receipt)
    tier_a = [c for c in candidates if c.match_reason == "exact_same_currency"]
    tier_b = [
        c
        for c in candidates
        if c.match_reason == "original_fx_description" and c.has_merchant_overlap
    ]

    winner = _select_unambiguous_winner(tier_a)
    if winner is None and not tier_a:
        winner = _select_unambiguous_winner(tier_b)

    if winner is not None:
        receipt.transaction_id = winner.tx.id
        receipt.status = "matched"
        db.commit()
        db.refresh(receipt)
        return ReceiptMatchOutcome(
            receipt_id=receipt.id,
            matched_transaction_id=winner.tx.id,
            status=receipt.status,
            match_reason=winner.match_reason,
            confidence=winner.confidence,
            candidate_count=len(candidates),
        )

    if receipt.status != "verified":
        receipt.status = "needs_review" if candidates else "unlinked"
        db.commit()
        db.refresh(receipt)

    return ReceiptMatchOutcome(
        receipt_id=receipt.id,
        matched_transaction_id=None,
        status=receipt.status,
        match_reason=None,
        confidence=None,
        candidate_count=len(candidates),
    )


def reconcile_unlinked_receipts(db: Session) -> int:
    """Evaluate all receipts without a linked transaction and return count newly matched."""
    unlinked_ids = list(
        db.scalars(select(Receipt.id).where(Receipt.transaction_id.is_(None))).all()
    )
    matched_count = 0
    for receipt_id in unlinked_ids:
        outcome = match_receipt_to_transaction(db, receipt_id)
        if outcome.matched_transaction_id is not None:
            matched_count += 1
    return matched_count


def _to_duplicate_match(
    existing: Receipt,
    *,
    matched_by: str,
) -> SemanticDuplicateMatch:
    return SemanticDuplicateMatch(
        receipt_id=existing.id,
        merchant=existing.merchant,
        receipt_date=existing.receipt_date,
        total_amount=float(existing.total_amount),
        currency=existing.currency,
        order_number=existing.order_number,
        document_type=existing.document_type,
        matched_by=matched_by,
    )


def _find_duplicate_by_order_number(
    db: Session,
    *,
    order_number: str,
    curr_upper: str,
    total_amount: float,
    document_type: str,
) -> Optional[Receipt]:
    by_order = db.scalars(
        select(Receipt).where(
            Receipt.document_type == document_type,
            func.lower(Receipt.order_number) == order_number.lower(),
            func.upper(Receipt.currency) == curr_upper,
        )
    ).all()
    for existing in by_order:
        if abs(float(existing.total_amount) - total_amount) <= 0.05:
            return existing
    return None


def find_semantic_duplicate_receipt(
    db: Session,
    *,
    merchant: str,
    receipt_date: date,
    total_amount: float,
    currency: str,
    order_number: Optional[str] = None,
    document_type: str = "receipt",
) -> Optional[SemanticDuplicateMatch]:
    """Find an existing duplicate receipt by order_number or merchant+date(+/-1d)+total+currency."""
    target_key = normalize_merchant_key(merchant)
    curr_upper = (currency or "NZD").strip().upper()
    if order_number and document_type != "payslip":
        matched = _find_duplicate_by_order_number(
            db,
            order_number=order_number,
            curr_upper=curr_upper,
            total_amount=total_amount,
            document_type=document_type,
        )
        if matched is not None:
            return _to_duplicate_match(matched, matched_by="order_number")

    nearby = db.scalars(
        select(Receipt).where(
            Receipt.document_type == document_type,
            Receipt.receipt_date >= receipt_date - timedelta(days=1),
            Receipt.receipt_date <= receipt_date + timedelta(days=1),
            func.upper(Receipt.currency) == curr_upper,
        )
    ).all()
    for existing in nearby:
        if abs(float(existing.total_amount) - total_amount) > 0.05:
            continue
        if (
            order_number
            and existing.order_number
            and existing.order_number.lower() != order_number.lower()
        ):
            continue
        if normalize_merchant_key(existing.merchant) == target_key:
            return _to_duplicate_match(existing, matched_by="merchant_date_amount")
    return None


def _item_row_to_snapshot(row: ReceiptItem) -> ReceiptItemSnapshot:
    return ReceiptItemSnapshot(
        id=row.id,
        receipt_id=row.receipt_id,
        product_id=row.product_id,
        item_name=row.item_name,
        brand=row.brand,
        quantity=float(row.quantity),
        unit_price=float(row.unit_price),
        discount_amount=float(row.discount_amount) if row.discount_amount is not None else None,
        total_price=float(row.total_price),
        category=row.category,
        domain=row.domain,
        product_type=row.product_type,
        tags=tuple(parse_rule_tags(row.tags)),
        volume_ml=row.volume_ml,
        notes=row.notes,
        inherited_tags=tuple(row.inherited_tags),
        created_at=row.created_at,
    )


def load_receipt_snapshot(db: Session, receipt_id: int) -> ReceiptSnapshot:
    """Load a receipt with items and return a detached immutable ReceiptSnapshot."""
    receipt = db.scalar(
        select(Receipt)
        .options(joinedload(Receipt.transaction), selectinload(Receipt.items))
        .where(Receipt.id == receipt_id)
    )
    if receipt is None:
        raise ReceiptNotFoundError(f"Receipt {receipt_id} not found")
    return ReceiptSnapshot(
        id=receipt.id,
        transaction_id=receipt.transaction_id,
        document_type=receipt.document_type,
        receipt_date=receipt.receipt_date,
        merchant=receipt.merchant,
        total_amount=float(receipt.total_amount),
        currency=receipt.currency,
        tax_amount=float(receipt.tax_amount) if receipt.tax_amount is not None else None,
        order_number=receipt.order_number,
        source_email_message_id=receipt.source_email_message_id,
        status=receipt.status,
        image_path=receipt.image_path,
        image_mime_type=receipt.image_mime_type,
        image_filename=receipt.image_filename,
        has_image=receipt.has_image,
        is_processed=bool(receipt.is_processed),
        raw_text=receipt.raw_text,
        notes=receipt.notes,
        items=tuple(_item_row_to_snapshot(item) for item in receipt.items),
        created_at=receipt.created_at,
        updated_at=receipt.updated_at,
    )
