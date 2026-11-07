"""FastAPI entrypoint for Plutus wealth and runway engine."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import accounts, admin, budgets, health, institutions, integrations, mcp, runway, transactions

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
app.include_router(institutions.router)
app.include_router(transactions.router)
app.include_router(budgets.router)
app.include_router(runway.router)
app.include_router(integrations.router)
app.include_router(admin.router)


# Mount Streamable HTTP MCP server at /mcp
try:
    mcp.install_mcp(app)
except Exception as exc:
    # Fail loud in production, log warning in lightweight test runners
    if settings.environment == "production":
        raise
    print(f"Warning: MCP server install skipped in non-production environment: {exc}")
