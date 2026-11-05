"""FastAPI entrypoint for Plutus wealth and runway engine."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import accounts, admin, health, integrations, mcp, runway, transactions

app = FastAPI(
    title="Plutus API",
    description="Central wealth, ledger, asset valuation, and runway engine for Yggdrasil.",
    version="1.0.0",
)

# CORS configuration allowing Midas PWA and local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    )

# Mount REST endpoints
app.include_router(health.router)
app.include_router(accounts.router)
app.include_router(transactions.router)
app.include_router(runway.router)
app.include_router(integrations.router)
app.include_router(admin.router)


@app.on_event("startup")
def init_data_if_empty():
    """Seed initial mock financial dataset if the database has zero accounts on startup."""
    try:
        from sqlalchemy import select
        from app.db import SessionLocal
        from app.models import Account
        from app.services.seed import seed_initial_data

        with SessionLocal() as db:
            existing_count = len(db.scalars(select(Account)).all())
            if existing_count == 0:
                print("Plutus database is empty. Auto-seeding initial mock financial dataset...")
                seed_initial_data(db)
                print("Plutus initial mock financial dataset seeded successfully.")
    except Exception as exc:
        print(f"Warning: could not check/auto-seed database on startup: {exc}")


# Mount Streamable HTTP MCP server at /mcp
try:
    mcp.install_mcp(app)
except Exception as exc:
    # Fail loud in production, log warning in lightweight test runners
    if settings.environment == "production":
        raise
    print(f"Warning: MCP server install skipped in non-production environment: {exc}")
