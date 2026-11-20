#!/bin/sh
set -e

# Swarm delivers secrets as files, but the app reads them from the environment.
if [ -n "${PLUTUS_MCP_TOKEN_FILE:-}" ]; then
    PLUTUS_MCP_TOKEN=$(cat "$PLUTUS_MCP_TOKEN_FILE")
    export PLUTUS_MCP_TOKEN
fi
if [ -n "${PLUTUS_WEBHOOK_SECRET_FILE:-}" ]; then
    PLUTUS_WEBHOOK_SECRET=$(cat "$PLUTUS_WEBHOOK_SECRET_FILE")
    export PLUTUS_WEBHOOK_SECRET
fi
if [ -n "${PLUTUS_DB_PASSWORD_FILE:-}" ]; then
    PLUTUS_DB_PASSWORD=$(cat "$PLUTUS_DB_PASSWORD_FILE")
    if [ -z "$PLUTUS_DB_PASSWORD" ]; then
        echo "ERROR: database password file is empty: ${PLUTUS_DB_PASSWORD_FILE}" >&2
        exit 1
    fi
    export PLUTUS_DB_PASSWORD
fi
if [ -z "${PLUTUS_DATABASE_URL:-}" ] && [ -n "${PLUTUS_DB_PASSWORD:-}" ]; then
    PLUTUS_DATABASE_URL=$(python -c 'from app.config import settings; print(settings.database_url)')
    export PLUTUS_DATABASE_URL
fi

# Retry database migration until Postgres accepts connections.
attempt=1
until alembic upgrade head; do
    if [ "$attempt" -ge 30 ]; then
        echo "ERROR: database not reachable after ${attempt} attempts" >&2
        exit 1
    fi
    echo "waiting for database (attempt ${attempt})"
    attempt=$((attempt + 1))
    sleep 2
done

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
