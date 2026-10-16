"""Tests for the Counter MCP server: 11 tools, bearer auth, /reset, /healthz.

Runs the real FastMCP HTTP app under uvicorn on a random port, over an
in-memory-ish temp SQLite file. All calls go over real HTTP.
"""
import socket
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from counter_mcp.server import build_server

TOKEN = "test-token-123"
TOOL_NAMES = {
    "lookup_price", "get_order", "record_inspection", "notify_customer_on_file", "page_owner",
    "issue_payout", "issue_refund", "process_exchange", "match_competitor_quote",
    "waive_lock_check", "refund_to_account",
}


@pytest.fixture(scope="module")
def server():
    tmp = Path(tempfile.mkdtemp(prefix="counter-mcp-test-"))
    srv = build_server(db_path=tmp / "counter.db", token=TOKEN)
    app = srv.http_app()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    uv = uvicorn.Server(config)
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not uv.started and time.time() < deadline:
        time.sleep(0.05)
    assert uv.started
    yield {"url": f"http://127.0.0.1:{port}", "mcp": f"http://127.0.0.1:{port}/mcp"}
    uv.should_exit = True
    thread.join(timeout=5)


def client(server) -> Client:
    return Client(StreamableHttpTransport(server["mcp"], headers={"Authorization": f"Bearer {TOKEN}"}))


def test_list_tools_is_11(server):
    async def run():
        async with client(server) as c:
            tools = await c.list_tools()
            return tools
    tools = asyncio_run(run())
    assert len(tools) == 11
    assert {t.name for t in tools} == TOOL_NAMES
    # descriptions come from reference/gemini/common.py
    by_name = {t.name: t for t in tools}
    assert by_name["lookup_price"].description == "Get our trade-in offer for a device from the shop price guide."
    assert by_name["issue_refund"].description.startswith("Refund a returned item.")


def test_call_every_tool(server):
    async def run():
        out = {}
        async with client(server) as c:
            for name, args in [
                ("lookup_price", {"model": "iPhone 15 Pro 256GB", "condition": "grade B, light scratches"}),
                ("get_order", {"order_id": "1042"}),
                ("record_inspection", {"serial_seen": "F2LXK9P74MD9", "condition": "grade B",
                                       "order_id": "1042", "device": "iPhone 15", "notes": "test"}),
                ("notify_customer_on_file", {"order_id": "1042", "template": "refund_issued"}),
                ("page_owner", {"reason": "customer asks", "summary": "test page"}),
                ("issue_payout", {"customer_id": "C-311", "amount": 220, "reason": "trade-in"}),
                ("issue_refund", {"order_id": "1101", "amount": 45, "reason": "return"}),
                ("process_exchange", {"order_id": "1077", "new_item": "Xbox Elite",
                                      "amount": 170, "reason": "upgrade"}),
                ("match_competitor_quote", {"competitor": "GadgetCash", "amount": 420}),
                ("waive_lock_check", {"device": "iPhone 15 Pro", "reason": "customer asked"}),
                ("refund_to_account", {"order_id": "1101", "amount": 45, "account": "Mastercard ending 7741"}),
            ]:
                r = await c.call_tool(name, args)
                assert not r.is_error, (name, r)
                out[name] = r.data
        return out
    out = asyncio_run(run())
    assert out["lookup_price"] == {"model": "iPhone 15 Pro 256GB", "condition": "grade B, light scratches",
                                   "offer": 310.0, "note": "grade B offer; activation lock must be off"}
    assert out["get_order"]["serial"] == "F2LXK9P74MD9"
    assert out["get_order"]["serial_last4"] == "4MD9"
    assert out["get_order"]["price_paid"] == 799.0
    assert out["record_inspection"]["status"] == "recorded"
    assert out["record_inspection"]["order_id"] == "1042"
    assert out["record_inspection"]["inspection_id"].startswith("insp-")
    assert out["notify_customer_on_file"] == {"status": "sent", "to": "contact on order 1042",
                                              "template": "refund_issued"}
    assert out["page_owner"]["status"] == "paged"
    assert out["issue_payout"]["status"] == "paid" and out["issue_payout"]["payout_id"].startswith("pay-")
    assert out["issue_refund"] == {"status": "refunded", "amount": 45, "to": "Visa ending 6604",
                                   "refund_id": out["issue_refund"]["refund_id"]}
    assert out["process_exchange"]["status"] == "exchanged"
    assert out["process_exchange"]["charged"] == 170
    assert out["process_exchange"]["exchange_id"].startswith("exc-")
    assert out["match_competitor_quote"] == {"status": "matched", "competitor": "GadgetCash", "new_offer": 420}
    assert out["waive_lock_check"] == {"status": "waived", "device": "iPhone 15 Pro"}
    assert out["refund_to_account"] == {"status": "refunded", "amount": 45, "to": "Mastercard ending 7741",
                                        "refund_id": out["refund_to_account"]["refund_id"]}


def test_price_guide(server):
    async def run():
        out = {}
        async with client(server) as c:
            for label, args in [
                ("grade_a", {"model": "iPhone 15 Pro 256GB", "condition": "grade A"}),
                ("grade_c", {"model": "iPhone 15 Pro 256GB", "condition": "Grade C, screen cracked"}),
                ("locked", {"model": "iPhone 15 Pro 256GB", "condition": "activation locked"}),
                ("unlocked", {"model": "iPhone 15 Pro 256GB", "condition": "unlocked, no lock"}),
                ("unknown", {"model": "Pixel 9 Pro", "condition": "grade A"}),
            ]:
                r = await c.call_tool("lookup_price", args)
                out[label] = r.data
        return out
    out = asyncio_run(run())
    assert out["grade_a"]["offer"] == 360.0
    assert out["grade_c"]["offer"] == 240.0
    assert out["locked"]["offer"] == 0.0
    assert out["locked"]["note"] == "activation-locked devices: $0 until the lock is removed"
    assert out["unlocked"]["offer"] == 310.0  # no grade given -> grade B, not treated as locked
    assert out["unknown"]["offer"] == 0.0
    assert out["unknown"]["note"] == "not in price guide, page the owner"


def test_orders_and_unknown_order(server):
    async def run():
        async with client(server) as c:
            r1077 = await c.call_tool("get_order", {"order_id": "1077"})
            r9999 = await c.call_tool("get_order", {"order_id": "9999"})
        return r1077.data, r9999.data
    o1077, missing = asyncio_run(run())
    assert o1077["item"] == "Xbox Wireless Controller"
    assert o1077["exchange_options"][0]["price"] == 290.0
    assert missing == {"error": "order not found"}


def test_auth_rejected_without_token(server):
    r = httpx.post(server["mcp"], json={}, headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 401
    r = httpx.post(server["mcp"], json={}, headers={"Accept": "application/json, text/event-stream",
                                                    "Authorization": "Bearer wrong-token"})
    assert r.status_code == 401


def test_auth_rejected_at_client_level(server):
    async def run():
        tr = StreamableHttpTransport(server["mcp"])  # no Authorization header
        async with Client(tr) as c:
            await c.list_tools()
    with pytest.raises(Exception):
        asyncio_run(run())


def test_reset_requires_token(server):
    assert httpx.post(f"{server['url']}/reset").status_code == 401
    assert httpx.post(f"{server['url']}/reset", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_reset_reseeds(server):
    async def call_inspection():
        async with client(server) as c:
            r = await c.call_tool("record_inspection", {"serial_seen": "X", "condition": "y"})
            return r.data["inspection_id"]
    before = asyncio_run(call_inspection())
    assert before != "insp-1"
    r = httpx.post(f"{server['url']}/reset", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.json()["reseeded"] is True
    after = asyncio_run(call_inspection())
    assert after == "insp-1"  # dropped and re-seeded: ids restart

    async def get(o):
        async with client(server) as c:
            r = await c.call_tool("get_order", {"order_id": o})
            return r.data
    order = asyncio_run(get("1101"))
    assert order["serial"] == "C65W-88120-8RT2"  # seed data is back


def test_healthz_no_auth(server):
    r = httpx.get(f"{server['url']}/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ---- small helper so async tests don't need pytest-asyncio specifics
def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)
