"""Background periodic sync scheduler for configured bank/wallet providers."""

# ============================================================================
# 1. IMPORTS & CONSTANTS
# ============================================================================
import asyncio
import logging
from typing import Callable

from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.schemas import SyncResult
from app.services.connectors.akahu import sync_akahu
from app.services.connectors.wise import sync_wise

logger = logging.getLogger("plutus.sync_scheduler")

DEFAULT_LOOKBACK_DAYS = 14


# ============================================================================
# 2. PURE SCHEDULE LOGIC (Zero I/O)
# ============================================================================
def get_configured_providers() -> list[str]:
    """Return provider keys that have required credentials configured."""
    providers: list[str] = []
    if settings.akahu_app_token and settings.akahu_user_token:
        providers.append("akahu")
    if settings.wise_api_token:
        providers.append("wise")
    return providers


# ============================================================================
# 3. SYNC RUNNER & LIFESPAN LOOP
# ============================================================================
PROVIDER_HANDLERS: dict[str, Callable[[Session, int], SyncResult]] = {
    "akahu": lambda db, days: sync_akahu(db, days=days),
    "wise": lambda db, days: sync_wise(db, days=days),
}


def run_scheduled_sync_once(
    *,
    db_factory: Callable[[], Session] = SessionLocal,
    handlers: dict[str, Callable[[Session, int], SyncResult]] = PROVIDER_HANDLERS,
    providers: list[str] | None = None,
    days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict[str, SyncResult | str]:
    """Execute one sync pass across all configured providers with error isolation."""
    targets = providers if providers is not None else get_configured_providers()
    outcomes: dict[str, SyncResult | str] = {}

    for provider in targets:
        handler = handlers.get(provider)
        if not handler:
            continue
        db = db_factory()
        try:
            res = handler(db, days)
            outcomes[provider] = res
            logger.info(
                "Scheduled sync [%s]: accounts=%d created=%d skipped=%d snapshots=%d",
                provider,
                res.accounts_synced,
                res.transactions_created,
                res.transactions_skipped,
                res.snapshots_updated,
            )
        except Exception as exc:
            outcomes[provider] = f"error: {exc}"
            logger.exception("Scheduled sync [%s] failed: %s", provider, exc)
        finally:
            db.close()

    return outcomes


async def periodic_sync_loop(stop_event: asyncio.Event, interval_hours: int) -> None:
    """Run periodic sync every interval_hours until stop_event is set."""
    interval_seconds = max(1, interval_hours) * 3600
    # Brief initial delay so DB migrations and Uvicorn startup settle first
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=30.0)
        return
    except asyncio.TimeoutError:
        pass

    while not stop_event.is_set():
        await asyncio.to_thread(run_scheduled_sync_once)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=float(interval_seconds))
        except asyncio.TimeoutError:
            continue
