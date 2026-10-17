"""Public /mcp: ArmorIQ's MCP registry scans it. Own bearer, read tools only, off when unset."""
from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from httpx import ASGITransport
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from counter_backend.config import Settings
from counter_backend.main import create_app

from conftest import FakeArmor, FakeMCP

SEEN: list[dict] = []


async def upstream_mcp(request: Request):
    body = await request.body()
    SEEN.append({"auth": request.headers.get("authorization"), "session": request.headers.get("mcp-session-id"),
                 "body": json.loads(body) if body else None})
    return JSONResponse({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}},
                        headers={"mcp-session-id": "sess-9"})


@pytest.fixture(scope="module")
def upstream_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = Starlette(routes=[Route("/mcp", upstream_mcp, methods=["GET", "POST", "DELETE"])])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=5)


def make_client(upstream: str, public_token: str = "pub-token") -> httpx.AsyncClient:
    settings = Settings(armor_api_key="k", armor_login="o@x", owner_password="p", mcp_token="internal-token",
                        mcp_public_token=public_token, mcp_url=upstream, web_dir=Path("/missing"))
    app = create_app(settings=settings, armor=FakeArmor(), mcp=FakeMCP(), slack=None,
                     token_minter=lambda: {"token": "x", "model": "m"})
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def rpc(method: str, **params) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


async def test_off_when_no_public_token(upstream_url):
    async with make_client(upstream_url, public_token="") as c:
        r = await c.post("/mcp", json=rpc("tools/list"))
    assert r.status_code in (404, 405)


async def test_needs_the_public_token(upstream_url):
    SEEN.clear()
    async with make_client(upstream_url) as c:
        for auth in (None, "Bearer internal-token", "Bearer wrong"):
            headers = {"Authorization": auth} if auth else {}
            r = await c.post("/mcp", json=rpc("tools/list"), headers=headers)
            assert r.status_code == 401
    assert SEEN == []


async def test_forwards_with_the_internal_token(upstream_url):
    SEEN.clear()
    async with make_client(upstream_url) as c:
        r = await c.post("/mcp", json=rpc("tools/list"),
                         headers={"Authorization": "Bearer pub-token", "Mcp-Session-Id": "sess-9"})
    assert r.status_code == 200 and r.json()["result"] == {"tools": []}
    assert r.headers["mcp-session-id"] == "sess-9"
    assert SEEN[-1]["auth"] == "Bearer internal-token"
    assert SEEN[-1]["session"] == "sess-9"
    assert SEEN[-1]["body"]["method"] == "tools/list"


async def test_read_tools_run_money_tools_do_not(upstream_url):
    SEEN.clear()
    async with make_client(upstream_url) as c:
        h = {"Authorization": "Bearer pub-token"}
        ok = await c.post("/mcp", json=rpc("tools/call", name="lookup_price", arguments={}), headers=h)
        assert ok.status_code == 200 and "error" not in ok.json()
        for tool in ("issue_refund", "process_exchange", "issue_payout", "record_inspection"):
            r = await c.post("/mcp", json=rpc("tools/call", name=tool, arguments={"amount": 1}), headers=h)
            assert r.json()["error"]["message"] == "this tool only runs through the Counter gate"
        batch = [rpc("tools/list"), rpc("tools/call", name="issue_refund", arguments={})]
        r = await c.post("/mcp", json=batch, headers=h)
        assert "error" in r.json()
    assert [s["body"]["params"]["name"] for s in SEEN if s["body"]["method"] == "tools/call"] == ["lookup_price"]


def test_public_token_is_derived_and_off_by_default(monkeypatch):
    from counter_backend.config import public_mcp_token
    monkeypatch.setenv("MCP_TOKEN", "internal-token")
    monkeypatch.delenv("MCP_PUBLIC", raising=False)
    assert Settings().mcp_public_token == ""
    monkeypatch.setenv("MCP_PUBLIC", "1")
    tok = Settings().mcp_public_token
    assert tok == public_mcp_token("internal-token") and tok and tok != "internal-token"
    monkeypatch.setenv("MCP_TOKEN", "")
    assert Settings().mcp_public_token == ""
