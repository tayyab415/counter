"""Case plans: intake plan, open_case, intent drift, the case lock, the order rule,
and the owner keeping every tool."""
from conftest import settle

from counter_backend import cases
from counter_backend.armor import describe
from counter_backend.tools import TOOL_NAMES

DRIFT = "not in the plan for this case: intent drift (tool-not-in-plan)"
RETURN_PLAN = ["get_order", "record_inspection", "issue_refund", "notify_customer_on_file", "page_owner",
               "refund_to_account"]


async def new_session(world, role="counter"):
    body = {"role": role} if role == "counter" else {"role": "owner", "password": "pw"}
    r = await world.client.post("/api/session", json=body)
    assert r.status_code == 200
    return world.store.sessions[r.json()["session_id"]], r.json()


async def call(world, session, name, args, camera_on=True):
    return await world.gate.handle_tool(session, name, args, camera_on)


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


# ---------------------------------------------------------------- intake
async def test_counter_session_starts_on_the_intake_plan(world):
    s, body = await new_session(world)
    assert world.armor.started[-1] == (s.identity, ["lookup_price", "get_order", "page_owner"], "counter intake")
    assert body["plan"]["case_type"] == "intake" and body["plan"]["label"] == "Plan: intake"
    assert body["plan"]["tools"] == ["lookup_price", "get_order", "page_owner"]
    r = await call(world, s, "get_order", {"order_id": "1101"})
    assert r["decision"]["action"] == "allow"


async def test_money_tool_before_a_case_is_drift(world):
    s, _ = await new_session(world)
    r = await call(world, s, "issue_refund", {"order_id": "1101", "amount": 45, "reason": "r"})
    d, resp = r["decision"], r["response"]
    assert (d["layer"], d["action"], d["reason"]) == ("ARMORIQ", "block", DRIFT)
    assert d["armoriq_reason"].startswith("tool-not-in-plan")
    assert resp["status"] == "blocked_by_policy" and resp["decided_by"] == "ArmorIQ"
    assert "No case is open" in resp["note"]
    assert not [c for c in world.mcp.calls if c[0] == "issue_refund"]


# ---------------------------------------------------------------- open_case
async def test_open_case_starts_the_return_plan_on_the_same_session(world):
    s, _ = await new_session(world)
    armor_session = s.armor_session
    q = world.store.subscribe(s.session_id)
    r = await call(world, s, "open_case", {"case_type": "return", "order_id": "#1101"})
    assert r["decision"] is None
    resp = r["response"]
    assert resp["status"] == "case_open" and resp["case_type"] == "return" and resp["order_id"] == "1101"
    assert resp["plan"] == RETURN_PLAN
    assert world.armor.plans == [(s.identity, RETURN_PLAN, "return order 1101")]
    assert s.armor_session is armor_session and len(world.armor.sessions) == 1
    # never checked by ArmorIQ, never sent to MCP
    assert not [c for c in world.armor.checks if "open_case" in c[0]]
    assert not [c for c in world.mcp.calls if c[0] == "open_case"]
    events = drain(q)
    plan = [e["data"] for e in events if e["type"] == "plan"]
    assert plan[-1]["label"] == "Plan: return order 1101" and plan[-1]["status"] == "open"
    assert plan[-1]["tools"] == RETURN_PLAN and plan[-1]["session_id"] == s.session_id
    card = [e["data"]["case"] for e in events if e["type"] == "case"][-1]
    assert card["kind"] == "return"


async def test_open_case_validates_its_args(world):
    s, _ = await new_session(world)
    r = await call(world, s, "open_case", {"case_type": "loan"})
    assert r["decision"]["layer"] == "SHOP_RULE" and "case_type must be" in r["response"]["reason"]
    r = await call(world, s, "open_case", {"case_type": "return"})
    assert r["response"]["reason"] == "a return needs the order number"
    r = await call(world, s, "open_case", {"case_type": "trade-in", "order_id": "1101"})
    assert r["response"]["status"] == "case_open" and r["response"]["case_type"] == "trade_in"
    assert r["response"]["order_id"] is None
    assert world.armor.plans[-1][2] == "trade_in"


async def test_open_case_fails_closed_if_the_plan_cannot_start(world):
    s, _ = await new_session(world)
    world.armor.raise_on_plan = True
    r = await call(world, s, "open_case", {"case_type": "return", "order_id": "1101"})
    assert r["response"]["status"] == "error"
    assert s.plan["case_type"] == "intake"
    r = await call(world, s, "issue_refund", {"order_id": "1101", "amount": 45, "reason": "r"})
    assert r["decision"]["reason"] == DRIFT


# ---------------------------------------------------------------- drift
async def test_payout_in_a_return_case_is_blocked_as_drift(world):
    world.slack.posted_blocks.clear()
    s, _ = await new_session(world)
    await call(world, s, "open_case", {"case_type": "return", "order_id": "1101"})
    r = await call(world, s, "issue_payout", {"customer_id": "C-311", "amount": 90, "reason": "loyalty"})
    d, resp = r["decision"], r["response"]
    assert (d["layer"], d["action"], d["reason"]) == ("ARMORIQ", "block", DRIFT)
    assert resp["note"].startswith("issue_payout is outside the open case (return for order 1101)")
    assert resp["open_case"] == "return for order 1101"
    assert not [c for c in world.mcp.calls if c[0] == "issue_payout"]
    await settle()
    assert world.slack.posted_blocks[-1]["reason"] == DRIFT
    # tools in the plan still go through
    r = await call(world, s, "issue_refund", {"order_id": "1101", "amount": 45, "reason": "r"})
    assert r["response"]["status"] == "refunded"


def test_drift_reason_in_plain_words():
    assert describe("tool-not-in-plan: 'counter__issue_payout' was not declared in the captured plan") == DRIFT
    assert describe("some-new-reason: x") == "some-new-reason: x"


# ---------------------------------------------------------------- case lock
async def test_a_second_case_is_refused_while_one_is_open(world):
    s, _ = await new_session(world)
    await call(world, s, "open_case", {"case_type": "return", "order_id": "1101"})
    r = await call(world, s, "open_case", {"case_type": "trade_in"})
    d = r["decision"]
    assert (d["layer"], d["action"]) == ("SHOP_RULE", "refused")
    assert r["response"]["reason"] == "a case is already open: return for order 1101"
    assert r["response"]["status"] == "refused_by_shop_rule"
    assert len(world.armor.plans) == 1
    # opening the same case again is a no-op, not a refusal
    r = await call(world, s, "open_case", {"case_type": "return", "order_id": "1101"})
    assert r["response"]["status"] == "case_open" and r["decision"] is None
    assert len(world.armor.plans) == 1


async def test_case_closes_when_its_money_tool_runs(world):
    s, _ = await new_session(world)
    await call(world, s, "open_case", {"case_type": "return", "order_id": "1101"})
    q = world.store.subscribe(s.session_id)
    r = await call(world, s, "issue_refund", {"order_id": "1101", "amount": 45, "reason": "r"})
    assert r["response"]["status"] == "refunded"
    assert s.plan["status"] == "closed"
    assert [e["data"]["status"] for e in drain(q) if e["type"] == "plan"] == ["closed"]
    # the closed case's plan stays in force for follow-ups
    r = await call(world, s, "notify_customer_on_file", {"order_id": "1101", "template": "refund_issued"})
    assert r["decision"]["action"] == "allow"
    r = await call(world, s, "open_case", {"case_type": "trade_in"})
    assert r["response"]["status"] == "case_open"
    assert world.armor.plans[-1][1] == cases.PLANS["trade_in"]


async def test_refused_or_blocked_money_tool_keeps_the_case_open(world):
    world.armor.rules = {"issue_refund": "block"}
    s, _ = await new_session(world)
    await call(world, s, "open_case", {"case_type": "return", "order_id": "1101"})
    await call(world, s, "issue_refund", {"order_id": "1101", "amount": 0, "reason": "r"})
    await call(world, s, "issue_refund", {"order_id": "1101", "amount": 45, "reason": "r"})
    assert s.plan["status"] == "open"


async def test_no_new_case_while_a_hold_waits_and_the_hold_closes_it(world):
    world.armor.rules = {"process_exchange": "hold"}
    s, _ = await new_session(world)
    await call(world, s, "open_case", {"case_type": "exchange", "order_id": "1077"})
    r = await call(world, s, "process_exchange", {"order_id": "1077", "new_item": "Elite", "amount": 170, "reason": "r"})
    assert r["decision"]["action"] == "hold"
    r = await call(world, s, "open_case", {"case_type": "trade_in"})
    assert r["response"]["reason"] == "waiting for the owner on an earlier request"
    assert len(world.armor.plans) == 1   # the approve re-check runs on this plan
    p = await world.gate.approve(r_pid(world), "Sam")
    assert p["status"] == "executed" and s.plan["status"] == "closed"
    r = await call(world, s, "open_case", {"case_type": "trade_in"})
    assert r["response"]["status"] == "case_open"


async def test_rejected_hold_closes_the_case(world):
    world.armor.rules = {"issue_refund": "hold"}
    s, _ = await new_session(world)
    await call(world, s, "open_case", {"case_type": "return", "order_id": "1101"})
    await call(world, s, "issue_refund", {"order_id": "1101", "amount": 150, "reason": "r"})
    p = await world.gate.reject(r_pid(world), "Sam")
    assert p["status"] == "rejected" and s.plan["status"] == "closed"


def r_pid(world) -> str:
    return next(p["pending_id"] for p in world.store.pending.values() if p["status"] == "waiting")


# ---------------------------------------------------------------- order rule
async def test_money_tools_must_use_the_case_order(world):
    s, _ = await new_session(world)
    await call(world, s, "open_case", {"case_type": "return", "order_id": "1101"})
    r = await call(world, s, "issue_refund", {"order_id": "1042", "amount": 45, "reason": "r"})
    d = r["decision"]
    assert (d["layer"], d["action"]) == ("SHOP_RULE", "refused")
    assert d["reason"] == "this case is the return for order 1101, not order 1042"
    assert not [c for c in world.armor.checks if c[0].endswith("issue_refund")]
    # refund_to_account stays out of shop rules so ArmorIQ's policy block shows
    world.armor.rules = {"refund_to_account": "block"}
    r = await call(world, s, "refund_to_account", {"order_id": "1042", "amount": 45, "account": "MC 7741"})
    assert r["decision"]["layer"] == "ARMORIQ" and r["decision"]["reason"] == "tool_not_in_allowlist"


async def test_exchange_case_binds_process_exchange_to_its_order(world):
    s, _ = await new_session(world)
    await call(world, s, "open_case", {"case_type": "exchange", "order_id": "1077"})
    r = await call(world, s, "process_exchange", {"order_id": "1101", "new_item": "x", "amount": 20, "reason": "r"})
    assert r["decision"]["reason"] == "this case is the exchange for order 1077, not order 1101"


# ---------------------------------------------------------------- owner
async def test_owner_keeps_the_plan_with_every_tool(world):
    s, body = await new_session(world, "owner")
    assert world.armor.started[-1][1] == TOOL_NAMES and body["plan"]["tools"] == TOOL_NAMES
    r = await call(world, s, "match_competitor_quote", {"competitor": "GadgetCash", "amount": 420})
    assert r["decision"]["action"] == "allow"
    r = await call(world, s, "open_case", {"case_type": "return", "order_id": "1101"})
    assert r["response"]["status"] == "case_open" and r["response"]["plan"] == TOOL_NAMES
    assert world.armor.plans == []   # no new ArmorIQ plan for the owner
    r = await call(world, s, "issue_payout", {"customer_id": "C-311", "amount": 90, "reason": "r"})
    assert r["decision"]["action"] == "allow"


# ---------------------------------------------------------------- next customer
async def test_operator_closes_the_case_back_to_intake(world):
    s, _ = await new_session(world)
    await call(world, s, "open_case", {"case_type": "trade_in"})
    r = await world.client.post("/api/case/close", json={"session_id": s.session_id})
    assert r.status_code == 200 and r.json()["case_type"] == "intake"
    assert world.armor.plans[-1] == (s.identity, cases.PLANS["intake"], "counter intake")
    r = await call(world, s, "open_case", {"case_type": "return", "order_id": "1101"})
    assert r["response"]["status"] == "case_open"
    r = await world.client.post("/api/case/close", json={"session_id": "nope"})
    assert r.status_code == 404


async def test_operator_cannot_close_a_case_under_a_hold(world):
    world.armor.rules = {"issue_payout": "hold"}
    s, _ = await new_session(world)
    await call(world, s, "open_case", {"case_type": "trade_in"})
    await call(world, s, "issue_payout", {"customer_id": "C-311", "amount": 170, "reason": "r"})
    r = await world.client.post("/api/case/close", json={"session_id": s.session_id})
    assert r.status_code == 409
