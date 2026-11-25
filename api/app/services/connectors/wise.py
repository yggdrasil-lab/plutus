"""Wise multi-currency balances and statement connector."""

# ============================================================================
# 1. IMPORTS & CONSTANTS
# ============================================================================
from datetime import date, datetime, timedelta
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.schemas import SyncResult
from app.services.ai_gateway import AiGateway
from app.services.connectors import (
    ConnectorConfigError,
    ConnectorUpstreamError,
    parse_iso_date,
)
from app.services.ingestion_service import (
    BankFeedGateway,
    ExternalAccountSpec,
    NormalizedTx,
    ProviderAccountFeed,
    resolve_utc_sync_window,
    sync_provider_feeds,
)

WISE_BASE_URL = "https://api.wise.com/v1"
WISE_V4_BASE_URL = "https://api.wise.com/v4"
WISE_MAX_WINDOW = timedelta(days=365)


def _iter_utc_windows(
    start_dt: datetime,
    end_dt: datetime,
    max_span: timedelta = WISE_MAX_WINDOW,
) -> list[tuple[datetime, datetime]]:
    """Split [start_dt, end_dt] into contiguous UTC windows of at most max_span."""
    if start_dt >= end_dt:
        return [(start_dt, end_dt)]
    windows: list[tuple[datetime, datetime]] = []
    cursor = start_dt
    while cursor < end_dt:
        window_end = min(cursor + max_span, end_dt)
        windows.append((cursor, window_end))
        cursor = window_end
    return windows


def _wise_tx_dedup_key(tx: dict) -> tuple:
    """Return a stable per-balance deduplication key for a Wise statement item."""
    ref = tx.get("referenceNumber")
    if ref:
        return ("ref", str(ref))
    amt = tx.get("amount") or {}
    details = tx.get("details") or {}
    return (
        "fallback",
        tx.get("date"),
        amt.get("value"),
        amt.get("currency"),
        details.get("description") or details.get("type"),
    )


def _append_unique_wise_txs(
    transactions: list[dict],
    seen_keys: set[tuple],
    merged_txs: list[dict],
) -> None:
    for tx in transactions:
        key = _wise_tx_dedup_key(tx)
        if key not in seen_keys:
            seen_keys.add(key)
            merged_txs.append(tx)


# ============================================================================
# 2. GATEWAY / ADAPTER (All HTTP I/O)
# ============================================================================
class HttpWiseGateway:
    """HTTP adapter for Wise REST API."""

    def __init__(self, api_token: str, timeout: float = 20.0) -> None:
        if not api_token:
            raise ConnectorConfigError("Wise credentials missing: set PLUTUS_WISE_API_TOKEN.")
        self._headers = {
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/json",
        }
        self._timeout = timeout

    def fetch_feeds(
        self,
        *,
        days: int = 30,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> list[ProviderAccountFeed]:
        """Fetch Wise profiles, balances, and chunked statements, then normalize."""
        start_dt, end_dt = resolve_utc_sync_window(
            days=days,
            start_date=start_date,
            end_date=end_date,
        )
        windows = _iter_utc_windows(start_dt, end_dt)

        feeds: list[ProviderAccountFeed] = []
        with httpx.Client(timeout=self._timeout) as client:
            profiles = self._get_json(client, f"{WISE_BASE_URL}/profiles", "Wise /profiles")
            for profile in profiles:
                feeds.extend(self._fetch_profile_feeds(client, profile, windows))
        return feeds

    def _fetch_profile_feeds(
        self,
        client: httpx.Client,
        profile: dict,
        windows: list[tuple[datetime, datetime]],
    ) -> list[ProviderAccountFeed]:
        profile_id = profile["id"]
        balances = self._get_json(
            client,
            f"{WISE_V4_BASE_URL}/profiles/{profile_id}/balances",
            "Wise /balances",
            params={"types": "STANDARD,SAVINGS"},
        )
        return [
            normalize_wise_feed(
                profile,
                bal,
                self._fetch_balance_transactions(client, profile_id, bal, windows),
            )
            for bal in balances
        ]

    def _fetch_balance_transactions(
        self,
        client: httpx.Client,
        profile_id: object,
        balance: dict,
        windows: list[tuple[datetime, datetime]],
    ) -> list[dict]:
        currency = (balance.get("currency") or "NZD").upper()
        seen_keys: set[tuple] = set()
        merged_txs: list[dict] = []
        url = f"{WISE_BASE_URL}/profiles/{profile_id}/balance-statements/{balance['id']}/statement.json"
        for win_start, win_end in windows:
            stmt = self._get_json(
                client,
                url,
                "Wise statement",
                params={
                    "currency": currency,
                    "intervalStart": win_start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "intervalEnd": win_end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "type": "COMPACT",
                },
            )
            _append_unique_wise_txs(stmt.get("transactions", []), seen_keys, merged_txs)
        return merged_txs

    def _get_json(
        self,
        client: httpx.Client,
        url: str,
        label: str,
        params: Optional[dict] = None,
    ) -> list | dict:
        resp = client.get(url, headers=self._headers, params=params)
        if resp.status_code >= 400:
            raise ConnectorUpstreamError(f"{label} failed ({resp.status_code}): {resp.text[:200]}")
        return resp.json()


# ============================================================================
# 3. PURE NORMALIZERS (Zero I/O)
# ============================================================================
def _extract_wise_merchant(tx: dict) -> tuple[str, str]:
    """Extract (merchant, raw_description) from a Wise statement transaction."""
    details = tx.get("details") or {}
    raw_desc = details.get("description") or tx.get("referenceNumber") or ""
    merchant_obj = details.get("merchant") or {}
    recipient_obj = details.get("recipient") or {}
    merchant = (
        merchant_obj.get("name")
        or recipient_obj.get("name")
        or details.get("senderName")
        or raw_desc
        or details.get("type")
        or "Wise Transaction"
    )
    return str(merchant), str(raw_desc or merchant)


def _normalize_wise_transaction(tx: dict, default_currency: str) -> Optional[NormalizedTx]:
    tx_amt_obj = tx.get("amount") or {}
    tx_amount = tx_amt_obj.get("value")
    if tx_amount is None:
        return None
    merchant, raw_desc = _extract_wise_merchant(tx)
    tx_currency = (tx_amt_obj.get("currency") or default_currency).upper()
    fee_val = (tx.get("totalFees") or {}).get("value")
    exchange_details = tx.get("exchangeDetails") or {}
    to_currency = ((exchange_details.get("toAmount") or {}).get("currency") or "").upper()
    raw_fx_rate = exchange_details.get("rate")
    fx_rate = (
        float(raw_fx_rate)
        if (raw_fx_rate is not None and tx_currency != "NZD" and to_currency == "NZD")
        else None
    )
    return NormalizedTx(
        transaction_date=parse_iso_date(tx.get("date")),
        amount=float(tx_amount),
        currency=tx_currency,
        merchant=merchant,
        category="General",
        raw_description=raw_desc,
        exchange_rate=fx_rate,
        fee_amount=abs(float(fee_val)) if fee_val is not None else None,
        notes=f"wise:{tx.get('referenceNumber', '')}" if tx.get("referenceNumber") else None,
    )


def normalize_wise_feed(
    profile: dict,
    balance: dict,
    raw_transactions: list[dict],
) -> ProviderAccountFeed:
    """Transform one Wise currency balance and its statement into ProviderAccountFeed."""
    profile_id = profile["id"]
    profile_type = (profile.get("type") or "PERSONAL").capitalize()
    balance_id = balance["id"]
    currency = (balance.get("currency") or "NZD").upper()
    raw_bal = (balance.get("amount") or {}).get("value")
    acc_type = "savings" if (balance.get("type") or "").upper() == "SAVINGS" else "checking"

    spec = ExternalAccountSpec(
        provider="wise",
        external_id=f"{profile_id}:{balance_id}",
        name=balance.get("name") or f"Wise {currency} ({profile_type})",
        institution="Wise",
        account_type=acc_type,
        tier="cash",
        currency=currency,
        current_balance=float(raw_bal) if raw_bal is not None else None,
        snapshot_notes=f"Synced from Wise profile {profile_id}",
    )

    txs = [
        item
        for tx in raw_transactions
        if (item := _normalize_wise_transaction(tx, currency)) is not None
    ]
    return ProviderAccountFeed(account=spec, transactions=tuple(txs))


# ============================================================================
# 4. ORCHESTRATION
# ============================================================================
def sync_wise(
    db: Session,
    *,
    days: int = 14,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    skip_llm: bool = False,
    gateway: Optional[BankFeedGateway] = None,
    ai_gateway: AiGateway | None = None,
) -> SyncResult:
    """Pull Wise profiles, currency balances, and statements via injected or HTTP gateway."""
    resolved_gateway = gateway or HttpWiseGateway(api_token=settings.wise_api_token)
    feeds = resolved_gateway.fetch_feeds(
        days=days,
        start_date=start_date,
        end_date=end_date,
    )
    return sync_provider_feeds(
        db,
        provider="wise",
        feeds=feeds,
        skip_llm=skip_llm,
        gateway=ai_gateway,
    )
