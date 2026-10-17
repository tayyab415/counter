"""Gate tests with fake ArmorIQ + fake MCP: every shop rule, allow/hold/block
mapping, fail closed, get_order serial strip."""
import pytest

from conftest import settle


async def call(world, session, name, args, camera_on=True):
    return await world.gate.handle_tool(session, name, args, camera_on)


# ---------------------------------------------------------------- unknown tool
async def test_unknown_tool(world):
    s = world.add_session()
    r = await call(world, s, "hack_the_bank", {"x": 1})
    assert r["response"] == {"status": "error", "reason": "unknown tool"}
    d = r["decision"]
    assert d["layer"] == "SHOP_RULE" and d["action"] == "refused"
    assert not world.armor.checks


# ---------------------------------------------------------------- amount rules
@pytest.mark.parametrize("name,args", [
    ("issue_payout", {"customer_id": "C-311", "amount": 0, "reason": "r"}),
    ("issue_payout", {"customer_id": "C-311", "reason": "r"}),                      # missing
    ("issue_refund", {"order_id": "1101", "amount": -45, "reason": "r"}),
    ("issue_refund", {"order_id": "1101", "amount": "abc", "reason": "r"}),         # non-numeric
    ("issue_refund", {"order_id": "1101", "amount": None, "reason": "r"}),
    ("process_exchange", {"order_id": "1077", "new_item": "x", "reason": "r"}),
    ("match_competitor_quote", {"competitor": "GadgetCash", "amount": -420}),
    ("refund_to_account", {"order_id": "1101", "account": "a", "amount": 0}),
])
async def test_amount_must_be_positive(world, name, args):
    s = world.add_session()
    r = await call(world, s, name, args)
    assert r["response"]["status"] == "refused_by_shop_rule"
    assert r["response"]["reason"] == "amount must be a positive number"
    assert r["decision"]["layer"] == "SHOP_RULE" and r["decision"]["action"] == "refused"
    assert not world.armor.checks  # ArmorIQ never asked


async def test_non_money_tools_skip_amount_rule(world):
    s = world.add_session()
    r = await call(world, s, "lookup_price", {"model": "iPhone 15 Pro 256GB", "condition": "grade B"})
    assert r["response"]["offer"] == 310.0
    r = await call(world, s, "waive_lock_check", {"device": "X"})
    assert r["response"]["status"] == "waived"


# ---------------------------------------------------------------- serial rule
async def test_serial_mismatch_refused(world):
    s = world.add_session()
    r = await call(world, s, "record_inspection",
                   {"order_id": "1042", "device": "iPhone 15", "serial_seen": "F2LXK9P7-WRONG", "condition": "grade B"})
    assert r["response"]["status"] == "recorded"
    checked = [c[0] for c in world.armor.checks]
    assert checked == ["record_inspection"]
    r = await call(world, s, "issue_refund", {"order_id": "1042", "amount": 799, "reason": "changed mind"})
    assert r["response"]["status"] == "refused_by_shop_rule"
    assert r["response"]["reason"] == "serial on camera doesn't match the order"
    assert r["decision"]["layer"] == "SHOP_RULE"
    assert len(world.armor.checks) == 1  # the refund never reached ArmorIQ


async def test_serial_match_allows_refund(world):
    s = world.add_session()
    await call(world, s, "record_inspection",
               {"order_id": "1042", "serial_seen": "f2lx k9p7 4md9 ".replace(" ", ""), "condition": "grade B"})
    r = await call(world, s, "issue_refund", {"order_id": "1042", "amount": 799, "reason": "changed mind"})
    assert r["response"]["status"] == "refunded"
    assert r["decision"]["layer"] == "ARMORIQ" and r["decision"]["action"] == "allow"


async def test_process_exchange_also_serial_checked(world):
    s = world.add_session()
    await call(world, s, "record_inspection",
               {"order_id": "1077", "serial_seen": "0457-2213-XXXX", "condition": "like new"})
    r = await call(world, s, "process_exchange",
                   {"order_id": "1077", "new_item": "Elite", "amount": 170, "reason": "upgrade"})
    assert r["response"]["reason"] == "serial on camera doesn't match the order"


async def test_no_inspection_no_serial_refusal(world):
    # the rule needs a recorded inspection for that order in this session
    s = world.add_session()
    r = await call(world, s, "issue_refund", {"order_id": "1042", "amount": 799, "reason": "return"})
    assert r["response"]["status"] == "refunded"


async def test_serial_rule_ignores_other_tools(world):
    world.armor.rules["refund_to_account"] = "block"
    world.armor.rules["match_competitor_quote"] = "block"
    world.armor.rules["waive_lock_check"] = "block"
    s = world.add_session()
    await call(world, s, "record_inspection",
               {"order_id": "1101", "serial_seen": "TOTALLY-WRONG", "condition": "used"})
    for name, args in [("refund_to_account", {"order_id": "1101", "amount": 45, "account": "card"}),
                       ("match_competitor_quote", {"competitor": "GadgetCash", "amount": 420}),
                       ("waive_lock_check", {"device": "iPhone"})]:
        r = await call(world, s, name, args)
        assert r["decision"]["layer"] == "ARMORIQ", name
        assert r["decision"]["action"] == "block", name


# ---------------------------------------------------------------- already refunded
async def test_second_refund_refused(world):
    s = world.add_session()
    r1 = await call(world, s, "issue_refund", {"order_id": "1101", "amount": 45, "reason": "return"})
    assert r1["response"]["status"] == "refunded"
    r2 = await call(world, s, "issue_refund", {"order_id": "1101", "amount": 45, "reason": "return"})
    assert r2["response"]["status"] == "refused_by_shop_rule"
    assert r2["response"]["reason"] == "this order was already refunded"
    assert r2["decision"]["layer"] == "SHOP_RULE"


async def test_refunded_flag_is_cross_session(world):
    s1 = world.add_session()
    await call(world, s1, "issue_refund", {"order_id": "1101", "amount": 45, "reason": "return"})
    s2 = world.add_session()
    r = await call(world, s2, "issue_refund", {"order_id": "1101", "amount": 45, "reason": "return"})
    assert r["response"]["reason"] == "this order was already refunded"


async def test_refunded_rule_leaves_refund_to_account_alone(world):
    world.armor.rules["refund_to_account"] = "block"
    s = world.add_session()
    await call(world, s, "issue_refund", {"order_id": "1101", "amount": 45, "reason": "return"})
    r = await call(world, s, "refund_to_account", {"order_id": "1101", "amount": 45, "account": "Mastercard ending 7741"})
    assert r["decision"]["layer"] == "ARMORIQ" and r["decision"]["action"] == "block"
    assert r["response"]["reason"] == "the counter may never use this (tool_not_in_allowlist)"


async def test_owner_refund_to_account_marks_order_refunded(world):
    s = world.add_session(role="owner")
    r = await call(world, s, "refund_to_account", {"order_id": "1101", "amount": 45, "account": "Mastercard ending 7741"})
    assert r["response"]["status"] == "refunded"
    r2 = await call(world, s, "issue_refund", {"order_id": "1101", "amount": 45, "reason": "return"})
    assert r2["response"]["reason"] == "this order was already refunded"


# ---------------------------------------------------------------- camera rule
@pytest.mark.parametrize("name,args", [
    ("record_inspection", {"serial_seen": "X", "condition": "y"}),
    ("issue_refund", {"order_id": "1101", "amount": 45, "reason": "r"}),
    ("process_exchange", {"order_id": "1077", "new_item": "x", "amount": 170, "reason": "r"}),
    ("issue_payout", {"customer_id": "C-311", "amount": 50, "reason": "r"}),
])
async def test_camera_off_refused(world, name, args):
    s = world.add_session()
    r = await call(world, s, name, args, camera_on=False)
    assert r["response"]["status"] == "refused_by_shop_rule"
    assert r["response"]["reason"] == "camera is off; I need to see the item"
    assert not world.armor.checks


async def test_camera_off_ok_for_other_tools(world):
    s = world.add_session()
    r = await call(world, s, "page_owner", {"reason": "x", "summary": "y"}, camera_on=False)
    assert r["response"]["status"] == "paged"


# ---------------------------------------------------------------- pending hold rule
async def test_second_call_with_pending_hold_refused(world):
    world.armor.rules["process_exchange"] = "hold"
    s = world.add_session()
    args = {"order_id": "1077", "new_item": "Elite", "amount": 170, "reason": "upgrade"}
    r1 = await call(world, s, "process_exchange", args)
    assert r1["response"]["status"] == "held_for_approval"
    assert r1["response"]["owner_paged"] is True
    assert r1["decision"]["layer"] == "ARMORIQ" and r1["decision"]["action"] == "hold"
    pending_id = r1["decision"]["pending_id"]
    assert world.store.pending[pending_id]["status"] == "waiting"

    # any other call to that tool while pending is refused, even with different args
    r2 = await call(world, s, "process_exchange", {**args, "amount": 150})
    assert r2["response"]["status"] == "refused_by_shop_rule"
    assert r2["response"]["reason"] == "waiting for the owner on an earlier request"
    assert r2["decision"]["layer"] == "SHOP_RULE"

    # but other tools still work
    r3 = await call(world, s, "page_owner", {"reason": "x", "summary": "y"})
    assert r3["response"]["status"] == "paged"

    # and another session is unaffected
    s2 = world.add_session()
    r4 = await call(world, s2, "process_exchange", args)
    assert r4["response"]["status"] == "held_for_approval"


# ---------------------------------------------------------------- mapping
async def test_allow_hold_block_mapping(world):
    world.armor.rules.update({"process_exchange": "hold", "match_competitor_quote": "block"})
    s = world.add_session()
    r = await call(world, s, "get_order", {"order_id": "1042"})
    assert r["decision"]["action"] == "allow" and r["decision"]["layer"] == "ARMORIQ"
    r = await call(world, s, "match_competitor_quote", {"competitor": "GadgetCash", "amount": 420})
    assert r["response"]["status"] == "blocked_by_policy"
    assert r["response"]["decided_by"] == "ArmorIQ"
    assert r["response"]["reason"] == "the counter may never use this (tool_not_in_allowlist)"
    assert r["response"]["note"] == "This account can't do this. Offer what you can do instead."
    assert r["decision"]["action"] == "block"
    assert r["decision"]["armoriq_reason"] == "tool_not_in_allowlist"
    r = await call(world, s, "process_exchange", {"order_id": "1077", "new_item": "Elite", "amount": 170, "reason": "u"})
    assert r["response"]["status"] == "held_for_approval"
    assert r["response"]["note"] == "Tell the customer the owner has to approve this; it is not done yet."


async def test_fail_closed_on_armor_error(world):
    world.armor.raise_on_check = True
    s = world.add_session()
    r = await call(world, s, "issue_refund", {"order_id": "1101", "amount": 45, "reason": "return"})
    assert r["response"]["status"] == "blocked_by_policy"
    assert r["response"]["reason"] == "policy check unavailable"
    assert r["decision"]["layer"] == "ARMORIQ" and r["decision"]["action"] == "block"
    assert "RuntimeError" in r["decision"]["armoriq_reason"]
    assert not any(c[0] == "issue_refund" for c in world.mcp.calls)


# ---------------------------------------------------------------- get_order strip
async def test_get_order_strips_serial(world):
    s = world.add_session()
    r = await call(world, s, "get_order", {"order_id": "1042"})
    assert "serial" not in r["response"] and "serial_last4" not in r["response"]
    assert r["response"]["serial_check"] == "done by the system when you record the inspection"
    assert r["response"]["item"] == "iPhone 15 128GB Black"
    assert r["decision"]["result"] == r["response"]
    # server side knows the last4 for the shop rule
    assert world.store.serial_last4["1042"] == "4MD9"


async def test_get_order_not_found(world):
    s = world.add_session()
    r = await call(world, s, "get_order", {"order_id": "9999"})
    assert r["response"] == {"error": "order not found"}


# ---------------------------------------------------------------- case card
async def test_case_card_built_from_results(world):
    s = world.add_session()
    await call(world, s, "get_order", {"order_id": "1101"})
    assert s.case["order"]["order_id"] == "1101"
    assert s.case["order"]["serial_last4"] == "8RT2"
    assert "serial" not in s.case["order"]
    await call(world, s, "record_inspection",
               {"order_id": "1101", "device": "charger", "serial_seen": "C65W-88120-8RT2", "condition": "unused"})
    assert s.case["inspection"] == {"serial_seen": "C65W-88120-8RT2", "condition": "unused", "serial_match": True}
    await call(world, s, "issue_refund", {"order_id": "1101", "amount": 45, "reason": "return"})
    assert s.case["kind"] == "return"
    assert s.case["outcome"]["status"] == "refunded"
    assert s.case["outcome"]["amount"] == 45


async def test_case_card_serial_match_false_and_blocked(world):
    world.armor.rules["match_competitor_quote"] = "block"
    s = world.add_session()
    await call(world, s, "record_inspection", {"order_id": "1042", "serial_seen": "NOPE-1234", "condition": "grade C"})
    assert s.case["inspection"]["serial_match"] is False
    await call(world, s, "match_competitor_quote", {"competitor": "GadgetCash", "amount": 420})
    assert s.case["kind"] == "trade_in"
    assert s.case["outcome"]["status"] == "blocked"
    assert s.case["outcome"]["detail"] == "tool_not_in_allowlist"


# ---------------------------------------------------------------- slack fan-out (fire and forget)
async def test_slack_post_hold_and_block(world):
    world.armor.rules.update({"process_exchange": "hold", "match_competitor_quote": "block"})
    s = world.add_session()
    await call(world, s, "match_competitor_quote", {"competitor": "GadgetCash", "amount": 420})
    r = await world.gate.handle_tool(s, "process_exchange",
                                     {"order_id": "1077", "new_item": "Elite", "amount": 170, "reason": "u"},
                                     True, still_b64="data:image/jpeg;base64,/9j/4AAQSkZJRg==", customer_said="hi")
    await settle()
    assert len(world.slack.posted_blocks) == 1
    assert world.slack.posted_blocks[0]["tool"] == "match_competitor_quote"
    assert len(world.slack.posted_holds) == 1
    pending, still, said = world.slack.posted_holds[0]
    assert pending["pending_id"] == r["decision"]["pending_id"]
    assert pending["has_still"] is True and "still" not in pending
    assert still.startswith(b"\xff\xd8\xff")
    assert said == "hi"


async def test_still_is_not_in_decisions(world):
    s = world.add_session()
    r = await call(world, s, "get_order", {"order_id": "1042"})
    assert "still" not in r["decision"]["args"]
