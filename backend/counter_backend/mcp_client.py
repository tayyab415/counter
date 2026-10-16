"""MCP tool client: the official `mcp` Python client over streamable HTTP.

One short-lived session per call keeps it robust to MCP server restarts.
"""
from __future__ import annotations

import json
import logging

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

log = logging.getLogger("counter.mcp")


class MCPToolError(RuntimeError):
    pass


class MCPToolClient:
    def __init__(self, base_url: str, token: str, timeout: float = 20.0):
        self.base = base_url.rstrip("/")
        self.url = self.base + "/mcp"
        self.token = token
        self.timeout = timeout

    async def call_tool(self, name: str, args: dict) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"}
        http_client = httpx2.AsyncClient(headers=headers, timeout=self.timeout)
        try:
            async with http_client:
                async with streamable_http_client(self.url, http_client=http_client) as (read, write):
                    async with ClientSession(read, write, read_timeout_seconds=self.timeout) as session:
                        await session.initialize()
                        result = await session.call_tool(name, arguments=dict(args or {}),
                                                         read_timeout_seconds=self.timeout)
        except MCPToolError:
            raise
        except Exception as e:
            raise MCPToolError(f"{type(e).__name__}: {e}") from e

        if getattr(result, "is_error", False):
            raise MCPToolError(self._text(result) or "tool reported an error")
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            return structured
        text = self._text(result)
        if not text:
            raise MCPToolError("empty tool result")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise MCPToolError(f"non-JSON tool result: {text[:200]}") from e
        if not isinstance(data, dict):
            raise MCPToolError(f"unexpected tool result type: {type(data).__name__}")
        return data

    @staticmethod
    def _text(result) -> str:
        parts = []
        for c in getattr(result, "content", None) or []:
            t = getattr(c, "text", None)
            if t:
                parts.append(t)
        return "\n".join(parts)

    async def reset(self) -> bool:
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(self.base + "/reset", headers={"Authorization": f"Bearer {self.token}"})
        if r.status_code != 200:
            raise MCPToolError(f"reset returned {r.status_code}")
        return True
