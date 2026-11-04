# Plutus

Central wealth, ledger, asset valuation, and financial runway engine for the Yggdrasil homelab ecosystem.

Named after Plutus, the Greek god of wealth and abundance, this service serves as the core telemetry and calculation engine for Area `10-Wealth`. Plutus models wealth not merely as static dollar balances, but as a dynamic **Time Horizon (Runway in Weeks)** based on weekly asset valuation, expected compounding real returns, and weekly burn rates.

## Core Capabilities

- **Financial Runway Engine**:
  - Calculates Emergency Runway (cash survival floor) and Compounding Growth Runway.
  - Detects perpetual freedom thresholds (when portfolio real yield exceeds weekly expenses).
  - Micro-feedback loop calculating "Time Banked" per week from budget surpluses.
- **Asset & Account Tracking**:
  - Multi-tier accounts: Cash/Savings, Liquid Investment Funds (PIE/ETFs), Crypto, and Locked Superannuation (KiwiSaver).
  - Weekly asset snapshots and multi-currency support (NZD base with USD/AUD conversion).
- **Document & Statement Pipeline**:
  - Ingests official bank and brokerage statements (PDF, CSV, OFX) with automated table extraction via `pdfplumber`.
  - Closing balance extraction as verified ground truth anchors.
  - Transaction deduplication hashing (SHA-256) preventing double-entry.
- **Ecosystem & Agent Integration**:
  - Streamable HTTP MCP server mounted at `/mcp` for Hermes Agent and the Court of Odin (Saga, Odin).
  - Machine-to-machine webhook endpoints (`/integrations/*`) for iOS Shortcuts and Apple Pay auto-logging.
  - Nightly PostgreSQL backup via Charon (`/opt/plutus/data`).
