"""Server-side cumulative payout aggregation for counterpart-attributed accounts.

Aggregates unlinked positive inflows under the Income Root Category subtree
(``Income`` plus any active child/descendant subcategories) grouped by
``counterpart_account_id`` in a single SQL query (zero N+1 queries).

Reporting Currency (NZD) conversion matches ``spending_projection`` /
``fx_service.to_nzd_amount`` semantics:
- ``NZD`` transactions contribute ``amount`` directly (never multiplied by FX).
- Non-NZD transactions with stored ``exchange_rate > 0`` contribute
  ``amount * exchange_rate`` inside the grouped SQL query.
- Non-NZD transactions missing ``exchange_rate`` trigger a targeted DB-only
  cached ``ExchangeRate`` lookup (no external HTTP calls during reads),
  falling back to ``to_nzd_amount`` (1:1 with a warning) only when no cached
  rate exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import logging
from typing import Optional, Sequence

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import Account, Transaction
from app.schemas import AccountRead
from app.services.category_taxonomy import (
    get_category_parent_map,
    get_descendant_category_names,
)
from app.services.fx_service import get_exchange_rate, to_nzd_amount

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccountPayoutSummary:
    """Immutable cumulative payout telemetry for a single Account."""

    payout_total_native: float = 0.0
    payout_total_nzd: float = 0.0
    payout_count: int = 0
    first_payout_date: Optional[date] = None
    last_payout_date: Optional[date] = None


class _DbCacheOnlyFxFetcher:
    """No-op FX fetcher preventing external network calls during Account reads."""

    def fetch_rate(
        self,
        *,
        rate_date: date,
        from_currency: str,
        to_currency: str,
    ) -> Optional[tuple[float, str]]:
        return None


_DB_ONLY_FX_FETCHER = _DbCacheOnlyFxFetcher()


def _coerce_date(value: object) -> Optional[date]:
    if value is None or isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError(f"Unsupported date aggregate value: {type(value).__name__}")


def get_income_category_names_lower(db: Session) -> frozenset[str]:
    """Return lowercase category names belonging to the Income Root Category subtree."""
    parent_map = get_category_parent_map(db)
    names = get_descendant_category_names("Income", parent_map)
    return frozenset(names or {"income"})


def _base_payout_filters(
    account_ids: Sequence[int],
    income_categories_lower: frozenset[str],
) -> tuple:
    cat_expr = func.lower(func.trim(func.coalesce(Transaction.category, "")))
    return (
        Transaction.counterpart_account_id.in_(account_ids),
        Transaction.linked_transaction_id.is_(None),
        Transaction.amount > 0,
        cat_expr.in_(sorted(income_categories_lower)),
    )


def _lookup_cached_rate(
    db: Session,
    rate_cache: dict[tuple[str, date], Optional[float]],
    code: str,
    tx_date: date,
) -> Optional[float]:
    key = (code, tx_date)
    if key in rate_cache:
        return rate_cache[key]
    resolved = get_exchange_rate(
        db,
        rate_date=tx_date,
        from_currency=code,
        to_currency="NZD",
        fetcher=_DB_ONLY_FX_FETCHER,
    )
    if resolved is None or resolved[0] <= 0:
        logger.warning(
            "Missing FX rate for %s on %s during account payout aggregation; "
            "matching to_nzd_amount 1:1 fallback.",
            code,
            tx_date,
        )
        rate_cache[key] = None
    else:
        rate_cache[key] = float(resolved[0])
    return rate_cache[key]


def _resolve_missing_fx_nzd_totals(
    db: Session,
    missing_account_ids: Sequence[int],
    income_categories_lower: frozenset[str],
) -> dict[int, float]:
    """Convert non-NZD payout rows missing stored exchange_rate via DB-cached FX rates."""
    if not missing_account_ids:
        return {}
    ccy_expr = func.upper(func.trim(func.coalesce(Transaction.currency, "NZD")))
    rows = db.execute(
        select(
            Transaction.counterpart_account_id,
            Transaction.transaction_date,
            Transaction.amount,
            Transaction.currency,
        ).where(
            *_base_payout_filters(missing_account_ids, income_categories_lower),
            ccy_expr != "NZD",
            Transaction.exchange_rate.is_(None) | (Transaction.exchange_rate <= 0),
        )
    ).all()

    rate_cache: dict[tuple[str, date], Optional[float]] = {}
    extra_nzd: dict[int, float] = {}
    for acct_id, tx_date, amount, currency in rows:
        if acct_id is None:
            continue
        code = (currency or "NZD").strip().upper()
        rate = _lookup_cached_rate(db, rate_cache, code, tx_date)
        extra_nzd[acct_id] = extra_nzd.get(acct_id, 0.0) + to_nzd_amount(
            float(amount), code, rate
        )
    return extra_nzd


def _query_grouped_payout_rows(
    db: Session,
    account_ids: Sequence[int],
    income_categories_lower: frozenset[str],
):
    ccy_expr = func.upper(func.trim(func.coalesce(Transaction.currency, "NZD")))
    has_valid_fx = Transaction.exchange_rate.is_not(None) & (Transaction.exchange_rate > 0)
    known_nzd_expr = case(
        (ccy_expr == "NZD", Transaction.amount),
        (has_valid_fx, Transaction.amount * Transaction.exchange_rate),
        else_=0.0,
    )
    missing_fx_flag = case(
        (
            (ccy_expr != "NZD")
            & (Transaction.exchange_rate.is_(None) | (Transaction.exchange_rate <= 0)),
            1,
        ),
        else_=0,
    )
    stmt = (
        select(
            Transaction.counterpart_account_id.label("account_id"),
            func.sum(Transaction.amount).label("total_native"),
            func.sum(known_nzd_expr).label("total_nzd_known"),
            func.count(Transaction.id).label("payout_count"),
            func.min(Transaction.transaction_date).label("first_date"),
            func.max(Transaction.transaction_date).label("last_date"),
            func.sum(missing_fx_flag).label("missing_fx_count"),
        )
        .where(*_base_payout_filters(account_ids, income_categories_lower))
        .group_by(Transaction.counterpart_account_id)
    )
    return db.execute(stmt).all()


def summarize_account_payouts(
    db: Session,
    account_ids: Sequence[int],
) -> dict[int, AccountPayoutSummary]:
    """Compute cumulative unlinked Income payout summaries keyed by account ID."""
    unique_ids = sorted({int(aid) for aid in account_ids if aid is not None})
    if not unique_ids:
        return {}

    income_cats = get_income_category_names_lower(db)
    rows = _query_grouped_payout_rows(db, unique_ids, income_cats)
    if not rows:
        return {}

    missing_ids = [
        int(row.account_id)
        for row in rows
        if row.account_id is not None and int(row.missing_fx_count or 0) > 0
    ]
    missing_nzd_by_acct = _resolve_missing_fx_nzd_totals(db, missing_ids, income_cats)

    summaries: dict[int, AccountPayoutSummary] = {}
    for row in rows:
        if row.account_id is None:
            continue
        acct_id = int(row.account_id)
        summaries[acct_id] = AccountPayoutSummary(
            payout_total_native=round(float(row.total_native or 0.0), 2),
            payout_total_nzd=round(
                float(row.total_nzd_known or 0.0) + missing_nzd_by_acct.get(acct_id, 0.0),
                2,
            ),
            payout_count=int(row.payout_count or 0),
            first_payout_date=_coerce_date(row.first_date),
            last_payout_date=_coerce_date(row.last_date),
        )
    return summaries


def build_account_reads(db: Session, accounts: Sequence[Account]) -> list[AccountRead]:
    """Serialize Account ORM rows into AccountRead DTOs enriched with payout telemetry."""
    if not accounts:
        return []
    summaries = summarize_account_payouts(db, [acc.id for acc in accounts])
    results: list[AccountRead] = []
    for acc in accounts:
        summary = summaries.get(acc.id, AccountPayoutSummary())
        results.append(
            AccountRead.model_validate(acc).model_copy(
                update={
                    "payout_total_native": summary.payout_total_native,
                    "payout_total_nzd": summary.payout_total_nzd,
                    "payout_count": summary.payout_count,
                    "first_payout_date": summary.first_payout_date,
                    "last_payout_date": summary.last_payout_date,
                }
            )
        )
    return results
