#!/usr/bin/env bash
# Start the MCP server (port 8001) and the backend (port 8000) for local dev.
# Secrets are read from ~/.config/counter/.env by both processes.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(dirname "$HERE")"
REPO_DIR="$(dirname "$BACKEND_DIR")"

MCP_PID=""
BE_PID=""
cleanup() {
  [ -n "$BE_PID" ] && kill "$BE_PID" 2>/dev/null || true
  [ -n "$MCP_PID" ] && kill "$MCP_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "mcp server   http://127.0.0.1:8001/mcp  (healthz http://127.0.0.1:8001/healthz)"
echo "backend      http://127.0.0.1:8000/api/healthz"
(
  cd "$REPO_DIR/mcp_server"
  exec uv run counter-mcp
) &
MCP_PID=$!

(
  cd "$BACKEND_DIR"
  exec uv run python -m counter_backend.main
) &
BE_PID=$!

# bash 3.2 (macOS default) has no `wait -n`; poll instead.
while kill -0 "$MCP_PID" 2>/dev/null && kill -0 "$BE_PID" 2>/dev/null; do
  sleep 1
done
wait 2>/dev/null || true
