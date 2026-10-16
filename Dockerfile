# Counter: one image, both Python services.
# The MCP server listens on 127.0.0.1:8001 (private to the container); the
# backend listens on 0.0.0.0:$PORT and serves web/ plus the API.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, so a code change only re-runs the (fast) project install.
COPY mcp_server/pyproject.toml mcp_server/uv.lock mcp_server/
COPY backend/pyproject.toml backend/uv.lock backend/
RUN cd /app/mcp_server && uv sync --frozen --no-dev --no-install-project \
 && cd /app/backend && uv sync --frozen --no-dev --no-install-project

COPY mcp_server/ mcp_server/
COPY backend/ backend/
COPY web/ web/
COPY deploy/ deploy/
RUN cd /app/mcp_server && uv sync --frozen --no-dev \
 && cd /app/backend && uv sync --frozen --no-dev \
 && chmod +x /app/deploy/start.sh

# Cloud Run sets PORT. The SQLite seed lives in /tmp (writable, in-memory).
ENV PORT=8080 \
    COUNTER_WEB_DIR=/app/web \
    COUNTER_DB=/tmp/counter.db \
    MCP_URL=http://127.0.0.1:8001 \
    MCP_HOST=127.0.0.1 \
    MCP_PORT=8001

EXPOSE 8080
CMD ["/app/deploy/start.sh"]
