"""What the gate sends to ArmorIQ: numeric amounts, and the console telemetry
(a tool run per allowed call, a flush per hold or block)."""
from conftest import settle


async def call(world, session, name, args, camera_on=True):
    return await world.gate.handle_tool(session, name, args, camera_on)


async def test_string_amount_reaches_armoriq_as_a_number(world):
    # ArmorIQ's `args.amount > 100` rule skips a string, so "170" must go out as 170
    s = world.add_session()
    await call(world, s, "issue_payout", {"customer_id": "C-311", "amount": "170", "reason": "trade-in"})
    await call(world, s, "issue_refund", {"order_id": "1101", "amount": "45.50", "reason": "r"})
    sent = {c[0]: c[1]["amount"] for c in world.armor.checks}
    assert sent == {"issue_payout": 170, "issue_refund": 45.5}
    assert isinstance(sent["issue_payout"], int)


async def test_allowed_call_is_reported_once(world):
    s = world.add_session()
    await call(world, s, "lookup_price", {"model": "iPhone 15 Pro 256GB", "condition": "grade B"})
    await settle()
    assert world.armor.reports == [("lookup_price", {"model": "iPhone 15 Pro 256GB", "condition": "grade B"}, True, False)]


async def test_hold_and_block_flush_without_a_tool_run(world):
    world.armor.rules = {"process_exchange": "hold", "refund_to_account": "block"}
    s = world.add_session()
    await call(world, s, "process_exchange", {"order_id": "1077", "new_item": "Elite", "amount": 170, "reason": "r"})
    await call(world, s, "refund_to_account", {"order_id": "1101", "account": "MC 7741", "amount": 45})
    await settle()
    assert world.armor.reports == []
    assert world.armor.flushes == 2


async def test_shop_rule_refusal_sends_nothing(world):
    s = world.add_session()
    await call(world, s, "issue_refund", {"order_id": "1101", "amount": 0, "reason": "r"})
    await settle()
    assert world.armor.reports == [] and world.armor.flushes == 0 and not world.armor.checks


async def test_approved_hold_is_reported_as_delegated(world):
    world.armor.rules = {"process_exchange": "hold"}
    s = world.add_session()
    r = await call(world, s, "process_exchange", {"order_id": "1077", "new_item": "Elite", "amount": 170, "reason": "r"})
    p = await world.gate.approve(r["decision"]["pending_id"], "Sam")
    await settle()
    assert p["status"] == "executed"
    assert [(t, ok, d) for t, _, ok, d in world.armor.reports] == [("process_exchange", True, True)]


def test_policy_reasons_in_plain_words():
    from counter_backend.armor import describe
    assert describe("hold:process_exchange#1") == "over $100 needs the owner (hold:process_exchange#1)"
    assert describe("block:issue_payout#0") == "over $350, the counter can't (block:issue_payout#0)"
    assert describe("block:refund_to_account#0").startswith("the counter may never use this")
    assert describe("allow:tools.default#0").startswith("owner: every tool allowed")
    assert describe("something new") == "something new"


def test_v1_policy_reasons_in_plain_words():
    # armor.io/v1 on api.armoriq.ai answers with these instead of v2 rule ids
    from counter_backend.armor import describe
    assert describe("allow_with_logging") == "allowed (allow_with_logging)"
    assert describe("allow_with_logging", "get_order") == "allowed (allow_with_logging)"
    for tool in ("issue_refund", "process_exchange", "issue_payout", "counter__issue_payout"):
        assert describe("allow_with_logging", tool) == "up to $100, allowed (allow_with_logging)"
    assert describe("amount_requires_approval") == "over $100 needs the owner (amount_requires_approval)"
    assert describe("amount_exceeds_max") == "over $350, the counter can't (amount_exceeds_max)"
    assert describe("tool_not_in_allowlist") == "the counter may never use this (tool_not_in_allowlist)"
    assert describe("enforcement_passed") == "owner: every tool allowed (enforcement_passed)"
    assert describe("approved_delegation") == "approved by the owner (approved_delegation)"
    assert describe("delegation_rejected") == \
        "the owner said no earlier in this conversation (delegation_rejected)"
    assert describe("tool-not-in-plan: 'counter__issue_payout' was not declared in the captured plan",
                    "issue_payout") == "not in the plan for this case: intent drift (tool-not-in-plan)"
    assert describe("") == "" and describe("enforce-unavailable: boom") == "enforce-unavailable: boom"


async def test_gate_passes_the_tool_so_money_allows_read_up_to_100(world):
    s = world.add_session()
    r = await call(world, s, "issue_refund", {"order_id": "1101", "amount": 45, "reason": "r"})
    assert r["decision"]["reason"] == "up to $100, allowed (allow_with_logging)"
    r = await call(world, s, "get_order", {"order_id": "1101"})
    assert r["decision"]["reason"] == "allowed (allow_with_logging)"
    world.armor.rules = {"issue_payout": "hold"}
    r = await call(world, s, "issue_payout", {"customer_id": "C-311", "amount": 170, "reason": "r"})
    assert r["response"]["reason"] == "over $100 needs the owner (amount_requires_approval)"


async def test_gemini_usage_recorded_on_the_session(world):
    s = world.add_session()
    r = await world.client.post("/api/usage", json={"session_id": s.session_id, "input_tokens": 812,
                                                     "output_tokens": 95, "cached_tokens": 0})
    assert r.status_code == 200
    await settle()
    assert world.armor.usage == [("gemini-3.1-flash-live-preview", 812, 95, 0)]
    r = await world.client.post("/api/usage", json={"session_id": "nope", "input_tokens": 1})
    assert r.status_code == 404
    r = await world.client.post("/api/usage", json={"session_id": s.session_id, "input_tokens": -5})
    assert r.status_code == 422
