"""FastAPI entrypoint for Plutus wealth and runway engine."""

import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import SessionLocal
from app.routers import accounts, budgets, categories, health, institutions, integrations, loans, mcp, products, receipts, rules, runway, transactions
from app.services.ai_gateway import MuninnHttpAdapter
from app.services.category_taxonomy import normalize_existing_legacy_categories
from app.services.fx_repair import run_startup_fx_repairs
from app.services.product_catalog import backfill_unlinked_receipt_item_products
from app.services.sync_scheduler import periodic_sync_loop


logger = logging.getLogger(__name__)


def _ensure_migrations_current() -> None:
    """Apply any pending Alembic migrations before serving traffic in production."""
    alembic_ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    if not alembic_ini.exists():
        raise RuntimeError(f"Alembic configuration not found at {alembic_ini}")
    cfg = AlembicConfig(str(alembic_ini))
    cfg.set_main_option("script_location", str(alembic_ini.parent / "alembic"))
    alembic_command.upgrade(cfg, "head")
    logger.info("Verified Alembic database migrations are at head.")


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    """Manage startup migrations, AI gateway lifecycle, and background periodic sync task."""
    ai_gateway = MuninnHttpAdapter.from_settings()
    app_instance.state.ai_gateway = ai_gateway
    stop_event = asyncio.Event()
    task = None
    if settings.environment == "production":
        _ensure_migrations_current()
        with SessionLocal() as db:
            updated = normalize_existing_legacy_categories(db)
            if updated:
                logger.info("Normalized %d legacy category records on startup.", updated)
            linked_products = backfill_unlinked_receipt_item_products(db)
            if linked_products:
                logger.info(
                    "Linked %d unlinked receipt items to catalog products on startup.",
                    linked_products,
                )
            repaired_fx, repaired_crypto = run_startup_fx_repairs(db)
            if repaired_fx:
                logger.info("Repaired %d transaction FX rates on startup.", repaired_fx)
            if repaired_crypto:
                logger.info("Repaired %d crypto transfer/FX records on startup.", repaired_crypto)
        if settings.sync_interval_hours > 0:
            task = asyncio.create_task(periodic_sync_loop(stop_event, settings.sync_interval_hours))
    try:
        yield
    finally:
        stop_event.set()
        if task:
            await task
        ai_gateway.close()
        app_instance.state.ai_gateway = None


app = FastAPI(
    title="Plutus API",
    description="Central wealth, ledger, asset valuation, and runway engine for Yggdrasil.",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS configuration allowing Midas PWA and local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Total-Count"],
)

# Mount REST endpoints
app.include_router(health.router)
app.include_router(accounts.router)
app.include_router(institutions.router)
app.include_router(transactions.router)
app.include_router(categories.router)
app.include_router(budgets.router)
app.include_router(loans.router)
app.include_router(rules.router)
app.include_router(receipts.router)
app.include_router(products.router)
app.include_router(runway.router)
app.include_router(integrations.router)


# Mount Streamable HTTP MCP server at /mcp
try:
    mcp.install_mcp(app)
except Exception as exc:
    # Fail loud in production, log warning in lightweight test runners
    if settings.environment == "production":
        raise
    print(f"Warning: MCP server install skipped in non-production environment: {exc}")
