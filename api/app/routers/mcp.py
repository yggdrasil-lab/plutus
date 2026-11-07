"""MCP server endpoint for Plutus wealth and runway engine."""

from __future__ import annotations

import contextlib
import json
from datetime import date, timedelta
from typing import Any

from fastapi import FastAPI
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.config import settings
from app.models import Account, AssetSnapshot, BudgetRule, Transaction
from app.routers.runway import get_effective_weekly_burn, get_latest_account_balances
from app.services.receipt_service import reconcile_unlinked_receipts
from app.services.runway import (
    calculate_compounding_runway,
    calculate_emergency_runway,
    calculate_time_banked,
    calculate_weekly_growth_rate,
)
from app.services.seed import clear_all_data, seed_initial_data
from app.services.statement_parser import generate_transaction_hash
from app.services.transfer_service import reconcile_transfers


class McpAuthMiddleware(BaseHTTPMiddleware):
    """Guard /mcp with bearer token; fail closed when unconfigured."""

    async def dispatch(self, request, call_next):
        if request.url.path.rstrip("/") == "/mcp":
            token = settings.mcp_token
            if not token:
                # Allow in development when no token is configured
                if settings.environment != "development":
                    return JSONResponse({"error": "mcp token not configured"}, status_code=503)
            else:
                scheme, _, presented = request.headers.get("authorization", "").partition(" ")
                if scheme.lower() != "bearer" or presented != token:
                    return JSONResponse(
                        {"error": "unauthorized"},
                        status_code=401,
                        headers={"WWW-Authenticate": "Bearer"},
                    )
        return await call_next(request)


def mcp_session() -> Session:
    from app.db import SessionLocal

    return SessionLocal()


def build_mcp_server() -> MCPServer:
    server = MCPServer(name="plutus", version="1.0.0")

    @server.tool(name="plutus_get_runway", description="Get current financial runway in weeks based on asset balances and weekly burn rate.")
    def plutus_get_runway(annual_real_return: float = 0.05) -> str:
        today = date.today()
        with mcp_session() as db:
            balances = get_latest_account_balances(db)
            weekly_burn, this_week_spend, target_budget = get_effective_weekly_burn(db, today)

            cash_bal = balances["cash"]
            growth_bal = balances["growth"]
            locked_bal = balances["locked"]
            total_liquid = cash_bal + growth_bal

            emergency_weeks = calculate_emergency_runway(cash_bal, weekly_burn)
            growth_weeks, is_perpetual = calculate_compounding_runway(
                accessible_assets=total_liquid,
                weekly_burn=weekly_burn,
                annual_real_return=annual_real_return,
            )

            weekly_g = calculate_weekly_growth_rate(annual_real_return)
            weekly_yield = round(total_liquid * weekly_g, 2)
            time_banked = calculate_time_banked(target_budget, this_week_spend)

            summary = {
                "as_of_date": today.isoformat(),
                "primary_runway_weeks": "Infinite (Perpetual Freedom)" if is_perpetual else (growth_weeks or emergency_weeks),
                "primary_runway_years": "Infinite" if is_perpetual else round((growth_weeks or emergency_weeks) / 52.0, 2),
                "is_perpetual": is_perpetual,
                "weekly_burn_rate_nzd": weekly_burn,
                "portfolio_weekly_yield_nzd": weekly_yield,
                "time_banked_this_week": time_banked,
                "breakdown": {
                    "emergency_cash_weeks": emergency_weeks,
                    "cash_balance_nzd": cash_bal,
                    "growth_balance_nzd": growth_bal,
                    "locked_balance_nzd": locked_bal,
                    "total_liquid_nzd": total_liquid,
                },
            }
            return json.dumps(summary, indent=2)

    @server.tool(name="plutus_get_weekly_burn", description="Get spending in the current week vs target budget and recent category totals.")
    def plutus_get_weekly_burn() -> str:
        today = date.today()
        with mcp_session() as db:
            weekly_burn, this_week_spend, target_budget = get_effective_weekly_burn(db, today)
            start_of_week = today - timedelta(days=today.weekday())

            # Category summary for this week
            cat_stmt = (
                select(Transaction.category, func.sum(Transaction.amount).label("total"))
                .where(
                    Transaction.transaction_date >= start_of_week,
                    Transaction.transaction_date <= today,
                    Transaction.amount < 0,
                )
                .group_by(Transaction.category)
            )
            cat_rows = db.execute(cat_stmt).all()
            category_spends = {row.category: abs(float(row.total or 0.0)) for row in cat_rows}

            res = {
                "start_of_week": start_of_week.isoformat(),
                "as_of_date": today.isoformat(),
                "this_week_spend_nzd": this_week_spend,
                "weekly_budget_target_nzd": target_budget,
                "remaining_budget_nzd": round(target_budget - this_week_spend, 2),
                "time_banked_weeks": calculate_time_banked(target_budget, this_week_spend),
                "categories": category_spends,
            }
            return json.dumps(res, indent=2)

    @server.tool(name="plutus_log_expense", description="Log a quick expense directly into Plutus.")
    def plutus_log_expense(amount: float, merchant: str, category: str = "General", notes: str = "") -> str:
        today = date.today()
        spend_amount = -abs(amount)
        with mcp_session() as db:
            account = db.scalar(select(Account).where(Account.account_type == "checking", Account.is_active == True))
            if not account:
                account = Account(name="Primary Checking", account_type="checking", tier="cash", institution="Bank", currency="NZD")
                db.add(account)
                db.commit()
                db.refresh(account)

            raw_hash = generate_transaction_hash(today, spend_amount, merchant, account.id)
            existing = db.scalar(select(Transaction).where(Transaction.raw_hash == raw_hash))
            if existing:
                return json.dumps({"status": "duplicate", "message": f"Transaction already logged on {today}"})

            tx = Transaction(
                transaction_date=today,
                account_id=account.id,
                amount=spend_amount,
                currency="NZD",
                merchant=merchant,
                category=category,
                raw_description=f"AgentLog: {merchant}",
                raw_hash=raw_hash,
                notes=notes,
            )
            db.add(tx)
            db.commit()
            db.refresh(tx)

            reconcile_unlinked_receipts(db)
            reconcile_transfers(db)
            db.refresh(tx)
            return json.dumps({"status": "success", "id": tx.id, "merchant": merchant, "amount": spend_amount, "category": category})

    @server.tool(name="plutus_record_snapshot", description="Record or update an asset balance snapshot for an account.")
    def plutus_record_snapshot(account_name: str, balance: float, snapshot_date: str = "") -> str:
        snap_dt = date.fromisoformat(snapshot_date) if snapshot_date else date.today()
        with mcp_session() as db:
            account = db.scalar(select(Account).where(Account.name.ilike(f"%{account_name}%"), Account.is_active == True))
            if not account:
                raise ToolError(f"Account '{account_name}' not found")

            existing = db.scalar(
                select(AssetSnapshot).where(AssetSnapshot.account_id == account.id, AssetSnapshot.snapshot_date == snap_dt)
            )
            if existing:
                existing.balance = balance
                existing.balance_nzd = balance * float(existing.exchange_rate)
                existing.source = "mcp"
                db.commit()
                return json.dumps({"status": "updated", "account": account.name, "balance": balance, "date": snap_dt.isoformat()})

            snap = AssetSnapshot(
                account_id=account.id,
                snapshot_date=snap_dt,
                balance=balance,
                balance_nzd=balance,
                source="mcp",
            )
            db.add(snap)
            db.commit()
            return json.dumps({"status": "created", "account": account.name, "balance": balance, "date": snap_dt.isoformat()})

    @server.tool(name="plutus_seed_test_data", description="Seed initial realistic financial baseline dataset for testing.")
    def plutus_seed_test_data() -> str:
        with mcp_session() as db:
            result = seed_initial_data(db)
            return json.dumps(result, indent=2)

    @server.tool(name="plutus_clear_data", description="Clear all transactions, snapshots, and budgets to prepare for real data.")
    def plutus_clear_data(clear_accounts: bool = False) -> str:
        with mcp_session() as db:
            result = clear_all_data(db, clear_accounts=clear_accounts)
            return json.dumps(result, indent=2)

    @server.tool(name="plutus_reconcile_transfers", description="Scan and pair unmatched internal and cross-currency (Wise) account transfers.")
    def plutus_reconcile_transfers() -> str:
        with mcp_session() as db:
            matched = reconcile_transfers(db)
            return json.dumps({"status": "success", "matched_transfers": matched})

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
