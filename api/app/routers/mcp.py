"""MCP server endpoint for Plutus wealth and runway engine."""

from __future__ import annotations

import contextlib
import hmac
import json
from datetime import date

from fastapi import FastAPI
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.config import settings
from app.models import Account, AssetSnapshot
from app.services.ai_gateway import AiGateway
from app.services.credit_telemetry import CreditFxResolutionError
from app.services.ingestion_service import upsert_account_snapshot
from app.services.ledger_entry import (
    MCP_EXPENSE_WRITE_POLICY,
    TransactionWriteInput,
    record_transaction,
)
from app.services.runway import (
    calculate_time_banked,
    get_runway_snapshot,
)
from app.services.spending_projection import project_burn
from app.services.transfer_service import reconcile_transfers


class McpAuthMiddleware(BaseHTTPMiddleware):
    """Guard /mcp with bearer token; fail closed when unconfigured."""

    async def dispatch(self, request, call_next):
        if request.url.path.rstrip("/") == "/mcp":
            token = settings.resolved_mcp_token
            if not token:
                # Allow in development when no token is configured
                if settings.environment != "development":
                    return JSONResponse({"error": "mcp token not configured"}, status_code=503)
            else:
                scheme, _, presented = request.headers.get("authorization", "").partition(" ")
                if scheme.lower() != "bearer" or not hmac.compare_digest(
                    presented.encode("utf-8"), token.encode("utf-8")
                ):
                    return JSONResponse(
                        {"error": "unauthorized"},
                        status_code=401,
                        headers={"WWW-Authenticate": "Bearer"},
                    )
        return await call_next(request)


def mcp_session() -> Session:
    from app.db import SessionLocal

    return SessionLocal()


def _session_scope(db: Session | None):
    """Yield an injected session for tests or open a fresh MCP DB session."""
    return contextlib.nullcontext(db) if db is not None else mcp_session()


def _run_plutus_get_runway(
    annual_real_return: float = 0.05,
    *,
    db: Session | None = None,
    reference_date: date | None = None,
) -> str:
    """Compute MCP runway summary JSON from the unified RunwaySnapshot service."""
    with _session_scope(db) as session:
        try:
            snapshot = get_runway_snapshot(
                session,
                as_of=reference_date,
                annual_return=annual_real_return,
            )
        except CreditFxResolutionError as exc:
            raise ToolError(str(exc)) from exc
        summary = {
            "as_of_date": snapshot.as_of_date.isoformat(),
            "primary_runway_weeks": (
                "Infinite (Perpetual Freedom)"
                if snapshot.is_perpetual
                else snapshot.primary_runway_weeks
            ),
            "primary_runway_years": (
                "Infinite" if snapshot.is_perpetual else snapshot.primary_runway_years
            ),
            "is_perpetual": snapshot.is_perpetual,
            "weekly_burn_rate_nzd": snapshot.effective_weekly_burn,
            "observed_weekly_burn_nzd": snapshot.observed_weekly_burn,
            "effective_weekly_burn_nzd": snapshot.effective_weekly_burn,
            "weekly_spend_this_week_nzd": snapshot.this_week_spend,
            "weekly_budget_target_nzd": snapshot.weekly_budget_target,
            "portfolio_weekly_yield_nzd": snapshot.perpetual_threshold_weekly_income,
            "time_banked_this_week": snapshot.time_banked_this_week,
            "breakdown": {
                "emergency_cash_weeks": snapshot.emergency_weeks,
                "growth_weeks": snapshot.growth_weeks,
                "locked_weeks": snapshot.locked_weeks,
                "cash_balance_nzd": snapshot.cash_balance,
                "growth_balance_nzd": snapshot.growth_balance,
                "locked_balance_nzd": snapshot.locked_balance,
                "total_liquid_nzd": snapshot.total_liquid_balance,
                "total_liabilities_nzd": snapshot.total_liabilities,
                "net_worth_nzd": snapshot.net_worth,
                "variable_weekly_burn_nzd": snapshot.variable_weekly_burn,
                "recurring_weekly_burn_nzd": snapshot.recurring_weekly_burn,
                "this_week_variable_spend_nzd": snapshot.this_week_variable_spend,
                "tier_labels": snapshot.tier_labels.as_dict(),
                "credit_backstop": snapshot.credit_backstop.as_dict(),
            },
        }
        return json.dumps(summary, indent=2)


def _run_plutus_get_weekly_burn(
    *,
    db: Session | None = None,
    reference_date: date | None = None,
) -> str:
    """Return current-week NZD spend and reconciled per-category breakdown via project_burn."""
    today = reference_date or date.today()
    with _session_scope(db) as session:
        burn = project_burn(session, reference_date=today)
        res = {
            "start_of_week": burn.start_of_week.isoformat(),
            "as_of_date": burn.as_of_date.isoformat(),
            "this_week_spend_nzd": burn.this_week_spend,
            "weekly_budget_target_nzd": burn.weekly_budget_target,
            "remaining_budget_nzd": round(burn.weekly_budget_target - burn.this_week_spend, 2),
            "time_banked_weeks": calculate_time_banked(burn.weekly_budget_target, burn.this_week_spend),
            "categories": {item.category: item.spend_nzd for item in burn.this_week_categories},
        }
        return json.dumps(res, indent=2)


def _run_plutus_log_expense(
    amount: float,
    merchant: str,
    category: str = "General",
    notes: str = "",
    *,
    db: Session | None = None,
    reference_date: date | None = None,
    ai_gateway: AiGateway | None = None,
) -> str:
    """Log a quick expense into the primary active checking account."""
    today = reference_date or date.today()
    with _session_scope(db) as session:
        result = record_transaction(
            session,
            TransactionWriteInput(
                transaction_date=today,
                amount=-abs(amount),
                currency=None,
                merchant=merchant,
                category=category,
                raw_description=f"AgentLog: {merchant}",
                notes=notes or None,
            ),
            policy=MCP_EXPENSE_WRITE_POLICY,
            ai_gateway=ai_gateway,
        )
        if not result.created:
            return json.dumps(
                {"status": "duplicate", "message": f"Transaction already logged on {today}"}
            )
        tx = result.transaction
        return json.dumps(
            {
                "status": "success",
                "id": tx.id,
                "merchant": tx.merchant,
                "amount": tx.amount,
                "category": tx.category,
            }
        )


def _run_plutus_record_snapshot(
    account_name: str,
    balance: float,
    snapshot_date: str = "",
    *,
    db: Session | None = None,
    reference_date: date | None = None,
) -> str:
    """Record or update an asset balance snapshot for a matching active account."""
    snap_dt = date.fromisoformat(snapshot_date) if snapshot_date else (reference_date or date.today())
    with _session_scope(db) as session:
        account = session.scalar(select(Account).where(Account.name.ilike(f"%{account_name}%"), Account.is_active == True))
        if not account:
            raise ToolError(f"Account '{account_name}' not found")
        existing_id = session.scalar(
            select(AssetSnapshot.id).where(AssetSnapshot.account_id == account.id, AssetSnapshot.snapshot_date == snap_dt)
        )
        upsert_account_snapshot(
            session, account=account, snapshot_date=snap_dt,
            balance=balance, currency=account.currency or "NZD", source="mcp",
        )
        return json.dumps({
            "status": "updated" if existing_id is not None else "created",
            "account": account.name,
            "balance": balance,
            "date": snap_dt.isoformat(),
        })


def _run_plutus_reconcile_transfers(*, db: Session | None = None) -> str:
    """Scan and pair unmatched internal and cross-currency account transfers."""
    with _session_scope(db) as session:
        return json.dumps({"status": "success", "matched_transfers": reconcile_transfers(session)})


def build_mcp_server() -> MCPServer:
    """Construct the FastMCP server and bind Plutus tool handlers."""
    server = MCPServer(name="plutus", version="1.0.0")

    @server.tool(name="plutus_get_runway", description="Get current financial runway in weeks based on asset balances and weekly burn rate.")
    def plutus_get_runway(annual_real_return: float = 0.05) -> str:
        return _run_plutus_get_runway(annual_real_return=annual_real_return)

    @server.tool(name="plutus_get_weekly_burn", description="Get spending in the current week vs target budget and recent category totals.")
    def plutus_get_weekly_burn() -> str:
        return _run_plutus_get_weekly_burn()

    @server.tool(name="plutus_log_expense", description="Log a quick expense directly into Plutus.")
    def plutus_log_expense(amount: float, merchant: str, category: str = "General", notes: str = "") -> str:
        return _run_plutus_log_expense(amount=amount, merchant=merchant, category=category, notes=notes)

    @server.tool(name="plutus_record_snapshot", description="Record or update an asset balance snapshot for an account.")
    def plutus_record_snapshot(account_name: str, balance: float, snapshot_date: str = "") -> str:
        return _run_plutus_record_snapshot(account_name=account_name, balance=balance, snapshot_date=snapshot_date)

    @server.tool(name="plutus_reconcile_transfers", description="Scan and pair unmatched internal and cross-currency (Wise) account transfers.")
    def plutus_reconcile_transfers() -> str:
        return _run_plutus_reconcile_transfers()

    return server


class _RebuildableApp:
    def __init__(self, server: MCPServer) -> None:
        self._server = server
        self.app: Starlette | None = None

    def _build(self) -> Starlette:
        return self._server.streamable_http_app(
            streamable_http_path="/mcp",
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            ),
        )

    async def __call__(self, scope, receive, send):
        if self.app is None:
            raise RuntimeError("MCP sub-app is not running; lifespan did not start")
        await self.app(scope, receive, send)


def install_mcp(app: FastAPI) -> None:
    """Attach the MCP server's route and auth middleware to FastAPI app."""
    server = build_mcp_server()
    holder = _RebuildableApp(server)

    app.router.routes.append(Route("/mcp", endpoint=holder))

    original_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(app_):
        sub_app = holder._build()
        holder.app = sub_app
        async with contextlib.AsyncExitStack() as stack:
            if original_lifespan is not None:
                await stack.enter_async_context(original_lifespan(app_))
            await stack.enter_async_context(sub_app.router.lifespan_context(app_))
            yield
        holder.app = None

    app.router.lifespan_context = lifespan
    app.add_middleware(McpAuthMiddleware)
