"""Shared provider sync registry and orchestration across HTTP routes and scheduled syncs."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Optional, Protocol

from sqlalchemy.orm import Session

from app.schemas import SyncResult
from app.services.ai_gateway import AiGateway
from app.services.connectors.akahu import sync_akahu
from app.services.connectors.bitcoin import sync_bitcoin
from app.services.connectors.wise import sync_wise


class ProviderSyncError(ValueError):
    """Base domain error for provider sync registry validation and dispatch."""


class InvalidSyncDateRangeError(ProviderSyncError):
    """Raised when start_date is after end_date."""


class UnsupportedSyncProviderError(ProviderSyncError):
    """Raised when a requested provider key is not registered."""


class ProviderSyncHandler(Protocol):
    """Callable contract for provider sync adapters."""

    def __call__(
        self,
        db: Session,
        days: int,
        start_date: Optional[date],
        end_date: Optional[date],
        skip_llm: bool,
        ai_gateway: AiGateway | None = None,
    ) -> SyncResult: ...


def _sync_akahu_adapter(
    db: Session,
    days: int,
    start_date: Optional[date],
    end_date: Optional[date],
    skip_llm: bool,
    ai_gateway: AiGateway | None = None,
) -> SyncResult:
    return sync_akahu(
        db,
        days=days,
        start_date=start_date,
        end_date=end_date,
        skip_llm=skip_llm,
        ai_gateway=ai_gateway,
    )


def _sync_wise_adapter(
    db: Session,
    days: int,
    start_date: Optional[date],
    end_date: Optional[date],
    skip_llm: bool,
    ai_gateway: AiGateway | None = None,
) -> SyncResult:
    return sync_wise(
        db,
        days=days,
        start_date=start_date,
        end_date=end_date,
        skip_llm=skip_llm,
        ai_gateway=ai_gateway,
    )


def _sync_bitcoin_adapter(
    db: Session,
    _days: int,
    start_date: Optional[date],
    end_date: Optional[date],
    skip_llm: bool,
    ai_gateway: AiGateway | None = None,
) -> SyncResult:
    return sync_bitcoin(
        db,
        start_date=start_date,
        end_date=end_date,
        skip_llm=skip_llm,
        ai_gateway=ai_gateway,
    )


SYNC_HANDLERS: dict[str, ProviderSyncHandler] = {
    "akahu": _sync_akahu_adapter,
    "wise": _sync_wise_adapter,
    "bitcoin": _sync_bitcoin_adapter,
}


def sync_provider(
    db: Session,
    provider: str,
    *,
    days: int = 30,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    skip_llm: bool = False,
    ai_gateway: AiGateway | None = None,
    handlers: Optional[Mapping[str, ProviderSyncHandler]] = None,
) -> SyncResult:
    """Validate sync window/provider and execute one or all registered provider handlers."""
    if start_date is not None and end_date is not None and start_date > end_date:
        raise InvalidSyncDateRangeError("start_date must be on or before end_date.")

    effective_handlers = SYNC_HANDLERS if handlers is None else handlers
    normalized = provider.strip().lower()
    if normalized != "all" and normalized not in effective_handlers:
        raise UnsupportedSyncProviderError(
            f"Unsupported sync provider '{provider}'. Use 'akahu', 'wise', 'bitcoin', or 'all'."
        )

    targets = (
        list(effective_handlers.values())
        if normalized == "all"
        else [effective_handlers[normalized]]
    )
    results = [
        handler(db, days, start_date, end_date, skip_llm, ai_gateway)
        for handler in targets
    ]
    if len(results) == 1:
        return results[0]

    return SyncResult(
        provider="all",
        accounts_synced=sum(r.accounts_synced for r in results),
        transactions_created=sum(r.transactions_created for r in results),
        transactions_skipped=sum(r.transactions_skipped for r in results),
        snapshots_updated=sum(r.snapshots_updated for r in results),
        accounts=[acc for r in results for acc in r.accounts],
    )
