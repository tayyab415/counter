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
