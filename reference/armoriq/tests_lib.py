"""Shared helpers for tests.py / tests2.py."""
import json, time, statistics
from sdkprobe import *
from setup import TOOLS

M = "counter__"
RES = HERE / "results.jsonl"
ts = int(time.time())
pending = set()
ARGS = {"lookup_price": {"device": "iPhone 13", "repair": "screen"}, "get_order": {"order_id": "R-1042"},
        "record_inspection": {"order_id": "R-1042", "notes": "cracked glass, no water damage"},
        "notify_customer_on_file": {"order_id": "R-1042", "message": "ready for pickup"},
        "page_owner": {"reason": "customer asks for manager"}, "waive_lock_check": {"order_id": "R-1042", "imei": "35-000000-000000-0"}}

def session_for(who):
    plan = [{"name": M + t, "args": ARGS.get(t, {"order_id": "R-1042", "amount": 1})} for t in TOOLS]
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        out, s, c = run(plan, [], mcp="counter", user_email=who, show_raw=False, obs=False)
    pv = (out["mint"] or {}).get("policy_validation") or {}
    print("   mint plan", (out["mint"] or {}).get("plan_id", "FAILED")[:8], "allowed_tools", len(pv.get("allowed_tools", [])), "denied", pv.get("denied_tools"))
    return s

def chk(s, who_label, who, test, tool, args, expect=None):
    RAW.clear()
    t0 = time.time(); r = s.check(M + tool, args, user_email=who); ms = int((time.time() - t0) * 1000)
    enf = next((x for x in RAW if x["url"].endswith("/iap/sdk/enforce")), None)
    dreq = next((x for x in RAW if "/delegation/request" in x["url"]), None)
    e = (enf or {}).get("resp") or {}
    enf_action = e.get("enforcementAction") if isinstance(e, dict) else None
    enf_reason = e.get("reason") if isinstance(e, dict) else None
    dmsg = None
    if dreq:
        d = dreq["resp"] if isinstance(dreq["resp"], dict) else {"text": str(dreq["resp"])}
        dmsg = f"{dreq['status']} {d.get('status')} {redact_str(str(d.get('message')))[:90]}"
    if r.delegation_id: pending.add(r.delegation_id)
    ok = None if expect is None else (r.action == expect[0] and (len(expect) < 2 or enf_reason == expect[1] or r.reason == expect[1]))
    rec = {"run_ts": ts, "test": test, "who": who_label, "tool": tool, "args": args, "sdk_action": r.action, "sdk_reason": redact_str(str(r.reason)),
           "enforce_action": enf_action, "enforce_reason": enf_reason, "matched": (e.get("matchedPolicy") or {}).get("name") if isinstance(e, dict) else None,
           "delegation_id": r.delegation_id, "delegation_request": dmsg, "ms": ms, "expect": expect, "pass": ok}
    with RES.open("a") as f: f.write(json.dumps(rec) + "\n")
    print(f"   [{test}] {who_label} {tool} {args.get('amount','')} -> {r.action} / {enf_reason or r.reason} "
          f"deleg={(r.delegation_id or '-')[:8]} {('dreq: '+dmsg) if dmsg else ''} {ms}ms {'PASS' if ok else ('FAIL' if ok is False else '')}")
    return r, rec

def reject(did, why="counter probe cleanup"):
    st, b = call("POST", "/delegation/reject", json_body={"delegationId": did, "approverEmail": LOGIN, "rejectionReason": why},
                 headers={"x-user-email": LOGIN}, label="reject", quiet=True)
    pending.discard(did)
    return st

def reject_all():
    for d in list(pending): print("   reject", d[:8], reject(d))

def approve(did):
    return call("POST", "/delegation/approve", json_body={"delegationId": did, "approverEmail": LOGIN, "approverRole": "org_admin"},
                headers={"x-user-email": LOGIN}, label="approve", quiet=True)

