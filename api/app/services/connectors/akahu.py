"""Akahu Personal App connector for New Zealand bank accounts and transactions."""

from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.schemas import SyncAccountSummary, SyncResult
from app.services.connectors import ConnectorConfigError, ConnectorUpstreamError
from app.services.ingestion_service import (
    NormalizedTx,
    get_or_create_external_account,
    ingest_normalized_transactions,
    upsert_account_snapshot,
)

AKAHU_BASE_URL = "https://api.akahu.io/v1"


def _map_akahu_account_type(akahu_type: str) -> tuple[str, str]:
    """Map Akahu account type to (plutus_account_type, plutus_tier)."""
    normalized = (akahu_type or "").upper()
    if normalized == "KIWISAVER":
        return "kiwisaver", "locked"
    if normalized in ("INVESTMENT", "SHARES", "SUPERANNUATION"):
        return "brokerage", "growth"
    if normalized == "CREDITCARD":
        return "credit_card", "cash"
    if normalized == "SAVINGS":
        return "savings", "cash"
    return "checking", "cash"


def _parse_iso_date(value: Optional[str]) -> date:
    """Parse Akahu ISO-8601 timestamp string into a local/UTC date."""
    if not value:
        return date.today()
    cleaned = value.replace("Z", "+00:00")
    return datetime.fromisoformat(cleaned).date()


def sync_akahu(
    db: Session,
    *,
    days: int = 14,
    client: Optional[httpx.Client] = None,
) -> SyncResult:
    """Pull accounts and transactions from Akahu Personal App into Plutus."""
    if not settings.akahu_app_token or not settings.akahu_user_token:
        raise ConnectorConfigError(
            "Akahu credentials missing: set PLUTUS_AKAHU_APP_TOKEN and PLUTUS_AKAHU_USER_TOKEN."
        )

    headers = {
        "Authorization": f"Bearer {settings.akahu_user_token}",
        "X-Akahu-Id": settings.akahu_app_token,
        "Accept": "application/json",
    }

    owns_client = client is None
    http = client or httpx.Client(timeout=20.0)
    try:
        acc_resp = http.get(f"{AKAHU_BASE_URL}/accounts", headers=headers)
        if acc_resp.status_code >= 400:
            raise ConnectorUpstreamError(
                f"Akahu /accounts failed ({acc_resp.status_code}): {acc_resp.text[:200]}"
            )
        akahu_accounts = acc_resp.json().get("items", [])

        account_map = {}
        account_summaries: dict[str, SyncAccountSummary] = {}
        snapshots_updated = 0
        today = date.today()

        for item in akahu_accounts:
            ext_id = item["_id"]
            acc_type, tier = _map_akahu_account_type(item.get("type", ""))
            institution = (item.get("connection") or {}).get("name") or "NZ Bank"
            balance_obj = item.get("balance") or {}
            currency = (balance_obj.get("currency") or "NZD").upper()
            current_balance = balance_obj.get("current")

            account = get_or_create_external_account(
                db,
                provider="akahu",
                external_id=ext_id,
                name=item.get("name") or f"{institution} {acc_type.capitalize()}",
                institution=institution,
                account_type=acc_type,
                tier=tier,
                currency=currency,
            )
            account_map[ext_id] = account

            if current_balance is not None:
                upsert_account_snapshot(
                    db,
                    account=account,
                    snapshot_date=today,
                    balance=float(current_balance),
                    currency=currency,
                    source="akahu",
                    notes="Synced from Akahu /v1/accounts",
                )
                snapshots_updated += 1

            account_summaries[ext_id] = SyncAccountSummary(
                account_id=account.id,
                account_name=account.name,
                institution=account.institution,
                currency=account.currency,
                balance=float(current_balance) if current_balance is not None else None,
            )

        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=days)
        params: dict[str, str] = {
            "start": start_dt.isoformat().replace("+00:00", "Z"),
            "end": end_dt.isoformat().replace("+00:00", "Z"),
        }

        tx_by_account: dict[str, list[NormalizedTx]] = {ext_id: [] for ext_id in account_map}
        cursor: Optional[str] = None
        while True:
            req_params = dict(params)
            if cursor:
                req_params["cursor"] = cursor
            tx_resp = http.get(f"{AKAHU_BASE_URL}/transactions", headers=headers, params=req_params)
            if tx_resp.status_code >= 400:
                raise ConnectorUpstreamError(
                    f"Akahu /transactions failed ({tx_resp.status_code}): {tx_resp.text[:200]}"
                )
            payload = tx_resp.json()
            for tx in payload.get("items", []):
                ext_acc_id = tx.get("_account")
                if ext_acc_id not in account_map:
                    continue
                merchant_obj = tx.get("merchant") or {}
                category_obj = tx.get("category") or {}
                raw_desc = tx.get("description") or ""
                merchant_name = merchant_obj.get("name") or raw_desc or "Unknown"
                tx_by_account[ext_acc_id].append(
                    NormalizedTx(
                        transaction_date=_parse_iso_date(tx.get("date")),
                        amount=float(tx["amount"]),
                        currency=account_map[ext_acc_id].currency,
                        merchant=merchant_name,
                        category=category_obj.get("name") or "General",
                        raw_description=raw_desc or None,
                        notes=f"akahu:{tx.get('_id', '')}" if tx.get("_id") else None,
                    )
                )
            cursor = (payload.get("cursor") or {}).get("next")
            if not cursor:
                break

        total_created = 0
        total_skipped = 0
        for ext_id, items in tx_by_account.items():
            created, skipped, _ = ingest_normalized_transactions(db, account_map[ext_id], items)
            total_created += created
            total_skipped += skipped
            account_summaries[ext_id].transactions_created = created
            account_summaries[ext_id].transactions_skipped = skipped

        return SyncResult(
            provider="akahu",
            accounts_synced=len(account_map),
            transactions_created=total_created,
            transactions_skipped=total_skipped,
            snapshots_updated=snapshots_updated,
            accounts=list(account_summaries.values()),
        )
    finally:
        if owns_client:
            http.close()
