"""Wise multi-currency balances and statement connector."""

# ============================================================================
# 1. IMPORTS & CONSTANTS
# ============================================================================
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.schemas import SyncResult
from app.services.connectors import ConnectorConfigError, ConnectorUpstreamError
from app.services.ingestion_service import (
    BankFeedGateway,
    ExternalAccountSpec,
    NormalizedTx,
    ProviderAccountFeed,
    sync_provider_feeds,
)

WISE_BASE_URL = "https://api.wise.com/v1"


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

    def fetch_feeds(self, *, days: int) -> list[ProviderAccountFeed]:
        """Fetch Wise profiles, balances, and statements, then normalize."""
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=days)
        interval_start = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        interval_end = end_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        feeds: list[ProviderAccountFeed] = []
        with httpx.Client(timeout=self._timeout) as client:
            profiles = self._get_json(client, f"{WISE_BASE_URL}/profiles", "Wise /profiles")
            for profile in profiles:
                profile_id = profile["id"]
                balances = self._get_json(
                    client,
                    f"{WISE_BASE_URL}/profiles/{profile_id}/balances",
                    "Wise /balances",
                    params={"types": "STANDARD,SAVINGS"},
                )
                for bal in balances:
                    currency = (bal.get("currency") or "NZD").upper()
                    stmt = self._get_json(
                        client,
                        f"{WISE_BASE_URL}/profiles/{profile_id}/balance-statements/{bal['id']}/statement.json",
                        "Wise statement",
                        params={
                            "currency": currency,
                            "intervalStart": interval_start,
                            "intervalEnd": interval_end,
                            "type": "COMPACT",
                        },
                    )
                    feeds.append(normalize_wise_feed(profile, bal, stmt.get("transactions", [])))
        return feeds

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
def _parse_iso_date(value: Optional[str]) -> date:
    """Parse Wise ISO-8601 timestamp into a date."""
    if not value:
        return date.today()
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


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

    txs: list[NormalizedTx] = []
    for tx in raw_transactions:
        tx_amt_obj = tx.get("amount") or {}
        tx_amount = tx_amt_obj.get("value")
        if tx_amount is None:
            continue
        merchant, raw_desc = _extract_wise_merchant(tx)
        fee_val = (tx.get("totalFees") or {}).get("value")
        fx_rate = (tx.get("exchangeDetails") or {}).get("rate")
        txs.append(
            NormalizedTx(
                transaction_date=_parse_iso_date(tx.get("date")),
                amount=float(tx_amount),
                currency=(tx_amt_obj.get("currency") or currency).upper(),
                merchant=merchant,
                category="General",
                raw_description=raw_desc,
                exchange_rate=float(fx_rate) if fx_rate is not None else None,
                fee_amount=abs(float(fee_val)) if fee_val is not None else None,
                notes=f"wise:{tx.get('referenceNumber', '')}" if tx.get("referenceNumber") else None,
            )
        )

    return ProviderAccountFeed(account=spec, transactions=tuple(txs))


# ============================================================================
# 4. ORCHESTRATION
# ============================================================================
def sync_wise(
    db: Session,
    *,
    days: int = 14,
    gateway: Optional[BankFeedGateway] = None,
) -> SyncResult:
    """Pull Wise profiles, currency balances, and statements via injected or HTTP gateway."""
    resolved_gateway = gateway or HttpWiseGateway(api_token=settings.wise_api_token)
    feeds = resolved_gateway.fetch_feeds(days=days)
    return sync_provider_feeds(db, provider="wise", feeds=feeds)
