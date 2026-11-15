"""Wise multi-currency balances and statement connector."""

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

WISE_BASE_URL = "https://api.wise.com/v1"


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
    sender_name = details.get("senderName")
    merchant = (
        merchant_obj.get("name")
        or recipient_obj.get("name")
        or sender_name
        or raw_desc
        or details.get("type")
        or "Wise Transaction"
    )
    return str(merchant), str(raw_desc or merchant)


def sync_wise(
    db: Session,
    *,
    days: int = 14,
    client: Optional[httpx.Client] = None,
) -> SyncResult:
    """Pull Wise profiles, currency balances, and statements into Plutus."""
    if not settings.wise_api_token:
        raise ConnectorConfigError(
            "Wise credentials missing: set PLUTUS_WISE_API_TOKEN."
        )

    headers = {
        "Authorization": f"Bearer {settings.wise_api_token}",
        "Accept": "application/json",
    }

    owns_client = client is None
    http = client or httpx.Client(timeout=20.0)
    try:
        prof_resp = http.get(f"{WISE_BASE_URL}/profiles", headers=headers)
        if prof_resp.status_code >= 400:
            raise ConnectorUpstreamError(
                f"Wise /profiles failed ({prof_resp.status_code}): {prof_resp.text[:200]}"
            )
        profiles = prof_resp.json()

        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=days)
        interval_start = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        interval_end = end_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        today = date.today()

        summaries: list[SyncAccountSummary] = []
        total_created = 0
        total_skipped = 0
        snapshots_updated = 0

        for profile in profiles:
            profile_id = profile["id"]
            profile_type = (profile.get("type") or "PERSONAL").capitalize()

            bal_resp = http.get(
                f"{WISE_BASE_URL}/profiles/{profile_id}/balances",
                headers=headers,
                params={"types": "STANDARD,SAVINGS"},
            )
            if bal_resp.status_code >= 400:
                raise ConnectorUpstreamError(
                    f"Wise /balances failed ({bal_resp.status_code}): {bal_resp.text[:200]}"
                )

            for bal in bal_resp.json():
                balance_id = bal["id"]
                currency = (bal.get("currency") or "NZD").upper()
                amount_obj = bal.get("amount") or {}
                current_balance = amount_obj.get("value")
                bal_type = (bal.get("type") or "STANDARD").upper()
                acc_type = "savings" if bal_type == "SAVINGS" else "checking"
                bal_name = bal.get("name") or f"Wise {currency} ({profile_type})"
                ext_id = f"{profile_id}:{balance_id}"

                account = get_or_create_external_account(
                    db,
                    provider="wise",
                    external_id=ext_id,
                    name=bal_name,
                    institution="Wise",
                    account_type=acc_type,
                    tier="cash",
                    currency=currency,
                )

                if current_balance is not None:
                    upsert_account_snapshot(
                        db,
                        account=account,
                        snapshot_date=today,
                        balance=float(current_balance),
                        currency=currency,
                        source="wise",
                        notes=f"Synced from Wise profile {profile_id}",
                    )
                    snapshots_updated += 1

                stmt_resp = http.get(
                    f"{WISE_BASE_URL}/profiles/{profile_id}/balance-statements/{balance_id}/statement.json",
                    headers=headers,
                    params={
                        "currency": currency,
                        "intervalStart": interval_start,
                        "intervalEnd": interval_end,
                        "type": "COMPACT",
                    },
                )
                if stmt_resp.status_code >= 400:
                    raise ConnectorUpstreamError(
                        f"Wise statement failed ({stmt_resp.status_code}): {stmt_resp.text[:200]}"
                    )

                stmt_data = stmt_resp.json()
                normalized: list[NormalizedTx] = []
                for tx in stmt_data.get("transactions", []):
                    tx_amt_obj = tx.get("amount") or {}
                    tx_amount = tx_amt_obj.get("value")
                    if tx_amount is None:
                        continue
                    merchant, raw_desc = _extract_wise_merchant(tx)
                    fees_obj = tx.get("totalFees") or {}
                    fee_val = fees_obj.get("value")
                    fx_obj = tx.get("exchangeDetails") or {}
                    fx_rate = fx_obj.get("rate")

                    normalized.append(
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

                created, skipped, _ = ingest_normalized_transactions(db, account, normalized)
                total_created += created
                total_skipped += skipped
                summaries.append(
                    SyncAccountSummary(
                        account_id=account.id,
                        account_name=account.name,
                        institution=account.institution,
                        currency=account.currency,
                        balance=float(current_balance) if current_balance is not None else None,
                        transactions_created=created,
                        transactions_skipped=skipped,
                    )
                )

        return SyncResult(
            provider="wise",
            accounts_synced=len(summaries),
            transactions_created=total_created,
            transactions_skipped=total_skipped,
            snapshots_updated=snapshots_updated,
            accounts=summaries,
        )
    finally:
        if owns_client:
            http.close()
