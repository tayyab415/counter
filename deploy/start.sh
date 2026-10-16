#!/usr/bin/env bash
# Cloud Run entry point. One container runs both services:
#   MCP server   http://127.0.0.1:8001    (loopback only)
#   backend      http://0.0.0.0:$PORT     (public; serves web/ and the API)
set -euo pipefail

: "${PORT:=8080}"
export MCP_HOST="${MCP_HOST:-127.0.0.1}"
export MCP_PORT="${MCP_PORT:-8001}"
export COUNTER_DB="${COUNTER_DB:-/tmp/counter.db}"
export COUNTER_WEB_DIR="${COUNTER_WEB_DIR:-/app/web}"
export MCP_URL="${MCP_URL:-http://127.0.0.1:8001}"

echo "counter: starting mcp server on ${MCP_HOST}:${MCP_PORT} (db ${COUNTER_DB})"
/app/mcp_server/.venv/bin/counter-mcp &
MCP_PID=$!
trap 'kill "$MCP_PID" 2>/dev/null || true; wait "$MCP_PID" 2>/dev/null || true' EXIT INT TERM

# The backend fails closed while the MCP server is down, so wait for healthz.
MCP_OK=""
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25; do
  if /app/mcp_server/.venv/bin/python -c "import json,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8001/healthz', timeout=2); raise SystemExit(0 if r.status == 200 else 1)" 2>/dev/null; then
    MCP_OK=1
    break
  fi
  sleep 0.4
done
if [ -n "$MCP_OK" ]; then echo "counter: mcp ready"; else echo "counter: mcp healthz never answered; backend will fail closed"; fi

echo "counter: starting backend on 0.0.0.0:${PORT} (web ${COUNTER_WEB_DIR})"
cd /app/backend
exec /app/backend/.venv/bin/python -m counter_backend.main
