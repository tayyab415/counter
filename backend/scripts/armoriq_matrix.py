#!/usr/bin/env python
"""Check counter-policy (armor.io/v2) in the ArmorTools workspace, straight through the SDK.

No backend needed. Uses ARMORIQ_API_KEY / ARMORIQ_LOGIN / BACKEND_ENDPOINT from
~/.config/counter/.env. Prints one line per check, redacts the key and the owner
email, and rejects every delegation it leaves pending.

    cd backend && uv run python scripts/armoriq_matrix.py

What it covers:
  tiers      $45 allow, $170 hold, $520 block on the money tools; three never-tools blocked
  edges      100 allow, 100.5 hold, 350 hold, 350.01 block; "170" as a string; -50 and 0
  owner      the owner login gets allow on everything
  approve    hold -> /delegation/approve as the owner -> re-check allows once -> next call holds again
  dedup      a second hold on the same tool returns the first delegation id
  sticky     after a reject, the same tool in the same plan blocks
  self       a requester cannot approve its own hold
Telemetry is off here so the console only shows real Counter sessions.
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.expanduser(os.environ.get("COUNTER_ENV_FILE", "~/.config/counter/.env")))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from armoriq_sdk import ArmorIQClient  # noqa: E402
from armoriq_sdk.session import ObservabilityConfig, SessionOptions  # noqa: E402

from counter_backend.tools import PLAN_ARGS, TOOL_NAMES  # noqa: E402

KEY = os.environ["ARMORIQ_API_KEY"]
LOGIN = os.environ["ARMORIQ_LOGIN"]
BASE = os.environ.get("BACKEND_ENDPOINT", "https://staging-api.armoriq.io").rstrip("/")
client = ArmorIQClient(api_key=KEY, backend_endpoint=BASE, agent_id="counter-front-desk")
pending: set[str] = set()


def red(s) -> str:
    return str(s).replace(KEY, "<KEY>").replace(LOGIN, "<OWNER>")


def session(email: str | None = None):
    email = email or f"counter-{uuid.uuid4().hex[:8]}@example.com"
    opts = SessionOptions(mode="sdk", default_mcp_name="counter",
                          observability=ObservabilityConfig(enabled=False))
    s = client.for_user(email).start_session(opts)
    s.start_plan([{"name": f"counter__{t}", "args": PLAN_ARGS[t]} for t in TOOL_NAMES], goal="counter front desk")
    return s, email


def check(s, email, tool, args, label=""):
    t0 = time.perf_counter()
    r = s.check(f"counter__{tool}", args, user_email=email)
    ms = int((time.perf_counter() - t0) * 1000)
    if r.delegation_id:
        pending.add(r.delegation_id)
    amt = args.get("amount", "")
    print(f"  {label:<26} {tool:<23} {amt!r:>7} -> {r.action:<5} {red(r.reason):<36} "
          f"deleg={(r.delegation_id or '-')[:8]} {ms}ms")
    return r


def delegation(action: str, did: str, **extra):
    body = {"delegationId": did, "approverEmail": extra.pop("as_email", LOGIN), **extra}
    r = httpx.post(f"{BASE}/delegation/{action}", json=body, timeout=30,
                   headers={"X-API-Key": KEY, "x-user-email": body["approverEmail"]})
    if action in ("approve", "reject") and r.status_code < 300:
        pending.discard(did)
    print(f"  {action} {did[:8]} -> {r.status_code} {red(r.text)[:110]}")
    return r


def main() -> int:
    print(f"ArmorIQ {BASE}")
    m = {"order_id": "1077", "customer_id": "C-311", "new_item": "Elite", "reason": "matrix"}
    print("tiers (counter)")
    s, e = session()
    check(s, e, "lookup_price", {"model": "iPhone 15 Pro 256GB", "condition": "grade B"}, "read tool")
    for tool in ("issue_refund", "process_exchange", "issue_payout"):
        for a in (45, 170, 520):
            check(s, e, tool, dict(m, amount=a), f"{tool} ${a}")
    for tool in ("match_competitor_quote", "waive_lock_check", "refund_to_account"):
        check(s, e, tool, dict(m, amount=170, competitor="GadgetCash", account="MC 7741"), "never-tool @170")
    print("edges (issue_payout, fresh counter)")
    s, e = session()
    for a in (100, 100.5, 350, 350.01, "170", -50, 0):
        check(s, e, "issue_payout", dict(m, amount=a), f"amount {a!r}")
    print("owner")
    so, eo = session(LOGIN)
    for tool, a in (("match_competitor_quote", 420), ("issue_payout", 520), ("refund_to_account", 45)):
        check(so, eo, tool, dict(m, amount=a, competitor="GadgetCash", account="MC 7741"), "owner")
    print("approve once, dedup, sticky reject (fresh counter)")
    s, e = session()
    r = check(s, e, "process_exchange", dict(m, amount=170), "hold")
    delegation("approve", r.delegation_id, approverRole="org_admin")
    check(s, e, "process_exchange", dict(m, amount=170), "re-check after approve")
    r2 = check(s, e, "process_exchange", dict(m, amount=170), "next call")
    r3 = check(s, e, "process_exchange", dict(m, amount=300), "second hold, $300")
    print(f"  same delegation id as the pending $170 hold: {r3.delegation_id == r2.delegation_id}")
    delegation("reject", r2.delegation_id, rejectionReason="matrix")
    check(s, e, "process_exchange", dict(m, amount=170), "after reject")
    check(s, e, "process_exchange", dict(m, amount=90), "after reject, $90")
    print("self-approval (fresh counter)")
    s, e = session()
    r = check(s, e, "issue_refund", dict(m, order_id="1101", amount=150), "hold")
    delegation("approve", r.delegation_id, approverRole="org_admin", as_email=e)
    print("cleanup")
    for did in list(pending):
        delegation("reject", did, rejectionReason="matrix cleanup")
    print(f"pending left: {len(pending)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
