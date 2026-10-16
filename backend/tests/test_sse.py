"""SSE over real HTTP: a session stream and the owner-wide stream receive
decision / pending / case / pending_resolved events."""
import asyncio
import socket
import threading
import time

import httpx
import uvicorn

from conftest import build_world


def start_server(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    return server, port


async def collect(port, params, pred, timeout=15):
    out = []
    async with httpx.AsyncClient(timeout=timeout) as c:
        async with c.stream("GET", f"http://127.0.0.1:{port}/api/events", params=params) as r:
            assert r.status_code == 200
            async for line in r.aiter_lines():
                out.append(line)
                if pred(out):
                    break
    return out


async def wait_subscribed(store, key, before=None):
    for _ in range(300):
        now = set(store._subs.get(key, ()))
        if now - (before or set()):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"no new subscriber for {key}")


async def test_session_stream():
    w = build_world()
    server, port = start_server(w.app)
    try:
        s = w.add_session()
        w.armor.rules["process_exchange"] = "hold"
        args = {"order_id": "1077", "new_item": "Elite", "amount": 170, "reason": "upgrade"}
        got_hold = asyncio.create_task(collect(port, {"session_id": s.session_id},
                                               lambda ls: any(l.startswith("data:") and '"has_still"' in l for l in ls)))
        await wait_subscribed(w.store, s.session_id)
        r = await w.gate.handle_tool(s, "process_exchange", args, True)
        pid = r["decision"]["pending_id"]
        lines = await asyncio.wait_for(got_hold, timeout=15)
        text = "\n".join(lines)
        assert "event: decision" in text
        assert '"action": "hold"' in text
        assert "event: case" in text
        assert '"status": "waiting_for_owner"' in text
        assert "event: pending" in text
        assert f'"pending_id": "{pid}"' in text
        assert '"has_still": false' in text

        before = set(w.store._subs.get(s.session_id, ()))
        got_resolved = asyncio.create_task(collect(port, {"session_id": s.session_id},
                                                   lambda ls: any(l.startswith("data:") and '"status": "executed"' in l for l in ls)))
        await wait_subscribed(w.store, s.session_id, before=before)
        await w.gate.approve(pid)
        lines = await asyncio.wait_for(got_resolved, timeout=15)
        text = "\n".join(lines)
        assert '"status": "executed"' in text
        assert '"charged": 170' in text
    finally:
        server.should_exit = True


async def test_owner_star_stream():
    w = build_world()
    server, port = start_server(w.app)
    try:
        owner_token = "tok-owner-sse"
        w.add_session(role="owner", owner_token=owner_token)
        s = w.add_session()
        got = asyncio.create_task(collect(port, {"session_id": "*", "token": owner_token},
                                          lambda ls: any(l.startswith("data:") and '"tool": "get_order"' in l for l in ls)))
        await wait_subscribed(w.store, "*")
        await w.gate.handle_tool(s, "get_order", {"order_id": "1042"}, True)
        lines = await asyncio.wait_for(got, timeout=15)
        text = "\n".join(lines)
        assert '"tool": "get_order"' in text
        assert f'"session_id": "{s.session_id}"' in text
    finally:
        server.should_exit = True
