#!/usr/bin/env python
"""Dev stub for the Counter web UI.

Serves web/ plus a fake backend API (CONTRACTS.md section 2) so the call screen
and the owner console can be built and checked while backend/ is built in
parallel. Nothing here talks to ArmorIQ or the MCP server.

Real:  /api/session mints a real Gemini ephemeral token with google-genai from
       GEMINI_API_KEY (~/.config/counter/.env).
Canned: policy decisions (rules from reference/gemini/common.py Session.call and
       CONTRACTS section 2), tool results, approval execution, case cards.

Run (repo root):
    uv run --python 3.12 --with fastapi --with uvicorn --with google-genai \
        --with python-dotenv python web/dev_stub.py
Then:  http://127.0.0.1:8100/            call screen
       http://127.0.0.1:8100/?mock=1    scripted six-beat replay, no mic
       http://127.0.0.1:8100/owner.html owner console (OWNER_PASSWORD)

Port 8100 by default ($STUB_PORT / $PORT): the real backend owns 8000 and the
MCP server 8001 on this machine.

Dev-only extras (not in CONTRACTS, ignored by the real backend):
    POST /api/tool?auto_approve=1   auto-approve a hold after ~2.5 s (mock beats)
    POST /api/session {role:"owner"} also returns "owner_token"
    GET  /api/_dev_state            counters + recent log lines for tests
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.util
import json
import os
import random
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

HERE = Path(__file__).resolve().parent            # web/
REPO = HERE.parent                                # repo root
load_dotenv(os.path.expanduser(os.environ.get("COUNTER_ENV_FILE", "~/.config/counter/.env")))

# --------------------------------------------------------------------------- #
# reference/gemini/common.py: the tested tools, orders and system prompt.
# --------------------------------------------------------------------------- #

_common_spec = importlib.util.spec_from_file_location(
    "counter_common", REPO / "reference" / "gemini" / "common.py")
common = importlib.util.module_from_spec(_common_spec)
_common_spec.loader.exec_module(common)

MODEL = "gemini-3.1-flash-live-preview"
SYSTEM_PROMPT = (REPO / "reference" / "gemini" / "system_prompt.txt").read_text().strip()
SYSTEM_PROMPT += ("\n\nCall the tool before you tell the customer anything is done; never "
                  "say a refund, payout or exchange happened unless a tool result says so.")

TOOLS_JSON = [t.model_dump(exclude_none=True) for t in common.TOOLS]
ALL_TOOLS = {t["name"] for t in TOOLS_JSON}
ORDERS = common.ORDERS

MONEY = {"issue_payout", "issue_refund", "process_exchange", "match_competitor_quote", "refund_to_account"}
HARD_BLOCK = {"match_competitor_quote", "waive_lock_check", "refund_to_account"}
SERIAL_RULES = {"issue_refund", "process_exchange"}
CAMERA_TOOLS = {"record_inspection", "issue_refund", "process_exchange", "issue_payout"}

FAST = os.environ.get("STUB_LATENCY", "").lower() in ("off", "0", "false", "1")

PRICE_GUIDE = {
    "iphone 15 pro 256gb": {"grade a": 360.0, "grade b": 310.0, "grade c": 240.0},
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def alnum(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def oid(x) -> str:
    return re.sub(r"\D", "", str(x or ""))


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def pick_ms(layer: str, action: str) -> int:
    """Rough ArmorIQ timings from reference/armoriq/FINDINGS.md (T6)."""
    if FAST:
        return random.randint(2, 8)
    if layer == "SHOP_RULE":
        return random.randint(20, 90)
    if action == "hold":
        return random.randint(1300, 1900)
    return random.randint(550, 950)


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #

class Store:
    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.owner_tokens: set[str] = set()
        self.decisions: list[dict] = []
        self.pendings: dict[str, dict] = {}
        self.refunded: set[str] = set()          # order ids refunded since reset
        self.cases: dict[str, dict] = {}         # session_id -> case card
        self.streams: list[dict] = []
        self.tool_calls = 0
        self.log: list[str] = []

    def reset(self):
        self.sessions.clear()
        self.decisions.clear()
        self.pendings.clear()
        self.refunded.clear()
        self.cases.clear()
        self.tool_calls = 0
        # owner tokens survive a demo reset: they are console credentials, not demo state


store = Store()


def log(line: str):
    entry = f"{datetime.now().strftime('%H:%M:%S')} {line}"
    store.log.append(entry)
    if len(store.log) > 400:
        del store.log[:200]
    print(entry, flush=True)


def emit(kind: str, data: dict):
    """SSE: deliver to session streams and to owner streams (filter '*')."""
    sid = data.get("session_id")
    body = json.dumps(data)
    for conn in list(store.streams):
        if conn["filter"] == "*" or conn["filter"] == sid:
            conn["queue"].put_nowait((kind, body))


def public_pending(p: dict) -> dict:
    out = {k: v for k, v in p.items() if k != "still"}
    out["has_still"] = bool(p.get("still"))
    return out


# --------------------------------------------------------------------------- #
# Case card (CONTRACTS section 2 `case`)
# --------------------------------------------------------------------------- #

CASE_OUTCOME = {"refunded": "refunded", "exchanged": "exchanged", "paid": "paid"}


def case_for(sid: str) -> dict:
    return store.cases.setdefault(sid, {"session_id": sid, "case": {"kind": None, "order": None,
                                                                   "inspection": None, "outcome": None}})


def case_emit(sid: str):
    emit("case", case_for(sid))


def case_order(sid: str, row: dict):
    c = case_for(sid)["case"]
    c["order"] = {"order_id": row.get("order_id"), "item": row.get("item"), "price_paid": row.get("price_paid"),
                  "payment": row.get("payment"), "serial_last4": row.get("serial_last4")}
    # a new order starts a new case
    c["kind"] = None
    c["inspection"] = None
    c["outcome"] = None
    case_emit(sid)


def case_inspection(sid: str, order_id: str, serial_seen: str, condition: str):
    c = case_for(sid)["case"]
    row = ORDERS.get(oid(order_id))
    match = None
    if row and serial_seen:
        match = alnum(serial_seen).endswith(alnum(row["serial_last4"]))
    c["inspection"] = {"serial_seen": serial_seen, "condition": condition, "serial_match": match}
    case_emit(sid)


def case_outcome(sid: str, status: str, amount, detail: str, kind: str | None = None):
    c = case_for(sid)["case"]
    if kind:
        c["kind"] = kind
    c["outcome"] = {"status": status, "amount": amount, "detail": detail}
    case_emit(sid)


# --------------------------------------------------------------------------- #
# Gate: CONTRACTS section 2, order of checks
# --------------------------------------------------------------------------- #

def refuse(reason: str):
    return ("SHOP_RULE", "refused", reason, None,
            {"status": "refused_by_shop_rule", "decided_by": "shop rules", "reason": reason})


def gate(sess: dict, name: str, args: dict, camera_on: bool):
    """Returns (layer, action, reason, armoriq_reason, response|None)."""
    sid = sess["id"]
    if name not in ALL_TOOLS:
        return ("SHOP_RULE", "refused", "unknown tool", None,
                {"status": "error", "reason": "unknown tool"})

    # 2. shop rules (ArmorIQ is not asked)
    if name in MONEY:
        amt = num(args.get("amount"))
        if amt is None or amt <= 0:
            return refuse("amount must be a positive number")
    if name in SERIAL_RULES:
        o, seen = oid(args.get("order_id")), sess["inspections"].get(oid(args.get("order_id")))
        if o in ORDERS and seen is not None and not alnum(seen).endswith(alnum(ORDERS[o]["serial_last4"])):
            return refuse("serial on camera doesn't match the order")
    if name == "issue_refund" and oid(args.get("order_id")) in store.refunded:
        return refuse("this order was already refunded")
    if name in CAMERA_TOOLS and not camera_on:
        return refuse("camera is off; I need to see the item")
    if any(p["session_id"] == sid and p["tool"] == name and p["status"] == "waiting"
           for p in store.pendings.values()):
        return refuse("waiting for the owner on an earlier request")

    # 3. ArmorIQ (variant B: blocked tools are not in "*".allowedTools)
    if sess["role"] == "owner":
        return "ARMORIQ", "allow", "allowed for owner", "enforcement_passed", None
    if name in HARD_BLOCK:
        return ("ARMORIQ", "block", "tool blocked for counter", "tool_not_in_allowlist",
                {"status": "blocked_by_policy", "decided_by": "ArmorIQ", "reason": "tool blocked for counter",
                 "note": "This account can't do this. Offer what you can do instead."})
    if name in MONEY:
        amt = num(args.get("amount")) or 0.0
        if amt > 350:
            return ("ARMORIQ", "block", "amount exceeds the counter limit", "amount_exceeds_max",
                    {"status": "blocked_by_policy", "decided_by": "ArmorIQ", "reason": "amount_exceeds_max",
                     "note": "This account can't do this. Offer what you can do instead."})
        if amt > 100:
            return ("ARMORIQ", "hold", "amount requires owner approval", "amount_requires_approval", None)
    return "ARMORIQ", "allow", "within policy", "allow_with_logging", None


def execute(sess: dict, name: str, args: dict) -> dict:
    """Canned tool results (CONTRACTS section 1 table)."""
    a = dict(args)
    if name == "get_order":
        row = ORDERS.get(oid(a.get("order_id")))
        if not row:
            return {"error": "order not found"}
        out = {k: v for k, v in row.items() if k not in ("serial", "serial_last4")}
        out["serial_check"] = "done by the system when you record the inspection"
        case_order(sess["id"], row)
        return out
    if name == "lookup_price":
        model = str(a.get("model", "")).strip()
        cond = str(a.get("condition", "")).lower()
        guide = PRICE_GUIDE.get(model.lower())
        if "lock" in cond.replace("unlocked", "").replace("not locked", "").replace("no lock", ""):
            return {"model": model, "condition": a.get("condition"), "offer": 0.0,
                    "note": "activation-locked devices: $0 until the lock is removed"}
        if not guide:
            return {"model": model, "condition": a.get("condition"), "offer": 0.0,
                    "note": "not in price guide, page the owner"}
        grade = next((g for g in ("grade a", "grade b", "grade c") if g in cond), "grade b")
        return {"model": model, "condition": a.get("condition"), "offer": guide[grade],
                "note": f"{grade} offer; activation lock must be off"}
    if name == "record_inspection":
        o = oid(a.get("order_id"))
        if o:
            sess["inspections"][o] = a.get("serial_seen", "")
        case_inspection(sess["id"], a.get("order_id", ""), a.get("serial_seen", ""), a.get("condition", ""))
        return {"status": "recorded", "inspection_id": "insp-" + uuid.uuid4().hex[:6], "order_id": o or None,
                "serial_seen": a.get("serial_seen"), "condition": a.get("condition")}
    if name == "notify_customer_on_file":
        return {"status": "sent", "to": f"contact on order {oid(a.get('order_id'))}", "template": a.get("template")}
    if name == "page_owner":
        return {"status": "paged", "eta": "the owner will answer on their phone"}
    if name == "issue_payout":
        return {"status": "paid", "amount": num(a.get("amount")), "customer_id": a.get("customer_id"),
                "payout_id": "po-" + uuid.uuid4().hex[:6]}
    if name == "issue_refund":
        o = oid(a.get("order_id"))
        if o:
            store.refunded.add(o)
        pay = (ORDERS.get(o) or {}).get("payment", "original payment")
        return {"status": "refunded", "amount": num(a.get("amount")), "to": pay,
                "refund_id": "rfnd-" + uuid.uuid4().hex[:6]}
    if name == "process_exchange":
        return {"status": "exchanged", "order_id": oid(a.get("order_id")), "new_item": a.get("new_item"),
                "charged": num(a.get("amount")), "exchange_id": "xchg-" + uuid.uuid4().hex[:6]}
    if name == "match_competitor_quote":
        return {"status": "matched", "competitor": a.get("competitor"), "new_offer": num(a.get("amount"))}
    if name == "waive_lock_check":
        return {"status": "waived", "device": a.get("device")}
    if name == "refund_to_account":
        o = oid(a.get("order_id"))
        if o:
            store.refunded.add(o)
        return {"status": "refunded", "amount": num(a.get("amount")), "to": a.get("account"),
                "refund_id": "rfnd-" + uuid.uuid4().hex[:6]}
    return {"status": "ok"}


OUTCOME_FOR_TOOL = {"issue_refund": ("refunded", "return"), "refund_to_account": ("refunded", "return"),
                    "process_exchange": ("exchanged", "exchange"), "issue_payout": ("paid", "trade_in")}


def case_after_execute(sid: str, name: str, args: dict, result: dict):
    if name in OUTCOME_FOR_TOOL:
        status, kind = OUTCOME_FOR_TOOL[name]
        amount = result.get("amount", result.get("charged"))
        detail = result.get("to") or result.get("new_item") or name
        case_outcome(sid, status, amount, str(detail), kind)


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

app = FastAPI(title="Counter dev stub")


@app.post("/api/session")
async def api_session(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    role = (body or {}).get("role", "counter")
    sid = "ses-" + uuid.uuid4().hex[:10]

    if role == "owner":
        pw = os.environ.get("OWNER_PASSWORD", "")
        if not pw or (body or {}).get("password") != pw:
            raise HTTPException(status_code=401, detail="wrong password")
        token = "own-" + uuid.uuid4().hex
        store.owner_tokens.add(token)
        label, email = "owner · Sam", "owner (ARMORIQ_LOGIN)"
        extra = {"owner_token": token}
    else:
        label = "counter@shop"
        email = f"counter-{uuid.uuid4().hex[:8]}@example.com"     # never logged
        extra = {}

    store.sessions[sid] = {"id": sid, "role": "owner" if role == "owner" else "counter",
                           "label": label, "email": email, "inspections": {}}
    log(f"session {sid} {label}")

    gemini = None
    err = None
    try:
        gemini = {"token": mint_gemini_token(), "model": MODEL}
    except Exception as exc:                       # token minting is the only real call here
        err = f"{type(exc).__name__}: {exc}"[:300]
        log(f"gemini token mint failed: {err}")

    return {**{"session_id": sid, "role": "owner" if role == "owner" else "counter",
               "identity_label": label, "gemini": gemini, "system_prompt": SYSTEM_PROMPT,
               "tools": TOOLS_JSON}, **extra, **({"gemini_error": err} if err else {})}


def mint_gemini_token() -> str:
    from google import genai
    now = datetime.now(timezone.utc)
    client = genai.Client(http_options={"api_version": "v1alpha"})
    tok = client.auth_tokens.create(config={
        "uses": 1,
        "expire_time": now + timedelta(minutes=30),
        "new_session_expire_time": now + timedelta(minutes=2),
    })
    return tok.name


@app.post("/api/tool")
async def api_tool(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(422, "bad json")
    sid = body.get("session_id") or ""
    sess = store.sessions.get(sid)
    if not sess:
        raise HTTPException(404, "unknown session_id")
    name = body.get("name") or ""
    args = body.get("args") if isinstance(body.get("args"), dict) else {}
    camera_on = bool(body.get("camera_on"))
    still = body.get("still")
    auto_approve = request.query_params.get("auto_approve") == "1"
    store.tool_calls += 1

    layer, action, reason, armoriq_reason, response = gate(sess, name, args, camera_on)
    ms = pick_ms(layer, action)
    await asyncio.sleep(ms / 1000.0)

    decision = {"id": "dec-" + uuid.uuid4().hex[:6], "ts": now_iso(), "session_id": sid, "role": sess["role"],
                "tool": name, "args": args, "layer": layer, "action": action, "reason": reason, "ms": ms}
    if armoriq_reason:
        decision["armoriq_reason"] = armoriq_reason
    if action == "allow" and response is None:
        result = execute(sess, name, args)
        response = result
        decision["result"] = result
        case_after_execute(sid, name, args, result)
    elif action == "hold":
        pid = "pnd-" + uuid.uuid4().hex[:8]
        pending = {"pending_id": pid, "session_id": sid, "tool": name, "args": args,
                   "delegation_id": "deleg-" + uuid.uuid4().hex[:8], "status": "waiting",
                   "created_ts": now_iso(), "customer_said": None,
                   "still": base64.b64decode(still) if still else None}
        store.pendings[pid] = pending
        decision["pending_id"] = pid
        decision["delegation_id"] = pending["delegation_id"]
        response = {"status": "held_for_approval", "decided_by": "ArmorIQ", "reason": reason,
                    "owner_paged": True,
                    "note": "Tell the customer the owner has to approve this; it is not done yet."}
        emit("pending", public_pending(pending))
        case_outcome(sid, "waiting_for_owner", num(args.get("amount")),
                     f"{name} · {reason}", "exchange" if name == "process_exchange" else None)
        if auto_approve:
            asyncio.create_task(_auto_approve(pid))
    else:                                          # block / refused
        amt = num(args.get("amount"))
        if action == "block":
            case_outcome(sid, "blocked", amt, f"{name} · {reason}")
        else:
            case_outcome(sid, "refused", amt, f"{name} · {reason}")

    store.decisions.append(decision)
    emit("decision", decision)
    log(f"tool {name} {json.dumps(args)[:90]} -> {action} ({layer}, {ms} ms)")
    return {"response": response, "decision": decision}


async def _auto_approve(pending_id: str, delay: float = 2.5):
    """Dev stub only: used by the mock replay to show the waiting card flip."""
    await asyncio.sleep(delay)
    p = store.pendings.get(pending_id)
    if p and p["status"] == "waiting":
        await _resolve(p, "executed")


async def _resolve(pending: dict, status: str) -> dict:
    sid = pending["session_id"]
    if status == "executed":
        result = execute(store.sessions[sid], pending["tool"], pending["args"])
        pending["status"], pending["result"] = "executed", result
        reason = "approved_delegation"
    else:
        result, reason = None, "owner declined"
        pending["status"] = "rejected"
    decision = {"id": "dec-" + uuid.uuid4().hex[:6], "ts": now_iso(), "session_id": sid,
                "role": store.sessions[sid]["role"], "tool": pending["tool"], "args": pending["args"],
                "layer": "ARMORIQ", "action": "allow" if status == "executed" else "block",
                "reason": reason, "armoriq_reason": reason, "ms": pick_ms("ARMORIQ", "allow"),
                "pending_id": pending["pending_id"], "delegation_id": pending["delegation_id"]}
    if result is not None:
        decision["result"] = result
        case_after_execute(sid, pending["tool"], pending["args"], result)
    else:
        case_outcome(sid, "refused", num(pending["args"].get("amount")),
                     f"{pending['tool']} · owner declined")
    store.decisions.append(decision)
    emit("decision", decision)
    emit("pending_resolved", {"pending_id": pending["pending_id"], "session_id": sid, "status": status,
                              "tool": pending["tool"], "result": result})
    log(f"pending {pending['pending_id']} -> {status}")
    return {**public_pending(pending), "result": result}


def require_owner(request: Request):
    token = request.headers.get("x-owner-token") or request.query_params.get("token")
    if not token or token not in store.owner_tokens:
        raise HTTPException(status_code=401, detail="owner login required")


@app.get("/api/pending")
async def api_pending(request: Request):
    require_owner(request)
    return {"pending": [public_pending(p) for p in sorted(store.pendings.values(), key=lambda p: p["created_ts"])]}


@app.get("/api/pending/{pid}/still.jpg")
async def api_pending_still(pid: str, request: Request):
    require_owner(request)
    p = store.pendings.get(pid)
    if not p or not p.get("still"):
        raise HTTPException(404, "no still")
    return Response(content=p["still"], media_type="image/jpeg")


@app.post("/api/pending/{pid}/approve")
async def api_approve(pid: str, request: Request):
    require_owner(request)
    p = store.pendings.get(pid)
    if not p:
        raise HTTPException(404, "unknown pending")
    if p["status"] != "waiting":
        raise HTTPException(409, "already resolved")
    return await _resolve(p, "executed")


@app.post("/api/pending/{pid}/reject")
async def api_reject(pid: str, request: Request):
    require_owner(request)
    p = store.pendings.get(pid)
    if not p:
        raise HTTPException(404, "unknown pending")
    if p["status"] != "waiting":
        raise HTTPException(409, "already resolved")
    return await _resolve(p, "rejected")


@app.get("/api/events")
async def api_events(request: Request, session_id: str = ""):
    sid = session_id or request.query_params.get("session_id", "")
    if sid == "*":
        require_owner(request)
    elif sid not in store.sessions:
        raise HTTPException(404, "unknown session")
    return StreamingResponse(_stream(sid), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _stream(sid: str):
    conn = {"queue": asyncio.Queue(), "filter": sid}
    store.streams.append(conn)
    try:
        yield ": counter stub stream\n\n"
        while True:
            try:
                kind, data = await asyncio.wait_for(conn["queue"].get(), timeout=15)
                yield f"event: {kind}\ndata: {data}\n\n"
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
    finally:
        with contextlib.suppress(ValueError):
            store.streams.remove(conn)


@app.post("/api/reset")
async def api_reset(request: Request):
    require_owner(request)
    store.reset()
    emit("reset", {"session_id": "*"})
    log("reset")
    return {"ok": True}


@app.get("/api/healthz")
async def healthz():
    return {"ok": True, "stub": True}


@app.get("/api/_dev_state")
async def dev_state():
    """Dev stub only: counters and the recent log, for the browser checks."""
    return {"tool_calls": store.tool_calls, "decisions": len(store.decisions),
            "pendings": [public_pending(p) for p in store.pendings.values()],
            "sessions": [{"id": s["id"], "role": s["role"], "label": s["label"]} for s in store.sessions.values()],
            "log": store.log[-120:]}


app.mount("/", StaticFiles(directory=str(HERE), html=True), name="web")


if __name__ == "__main__":
    port = int(os.environ.get("STUB_PORT", os.environ.get("PORT", "8100")))
    log(f"Counter dev stub on http://127.0.0.1:{port}/ (web dir {HERE})")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
