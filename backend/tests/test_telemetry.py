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


async def test_non_finite_amounts_are_refused_not_500(world):
    s = world.add_session()
    for bad in ("NaN", "Infinity", "-Infinity", "1e309"):
        r = await world.client.post("/api/tool", json={
            "session_id": s.session_id, "name": "issue_payout", "camera_on": True,
            "args": {"customer_id": "C-311", "amount": bad, "reason": "trade-in"}})
        assert r.status_code == 200, bad
        assert r.json()["decision"]["action"] == "refused"
        assert r.json()["response"]["reason"] == "amount must be a positive number"
    assert not world.armor.checks and not world.mcp.calls


async def test_ended_call_closes_its_armoriq_session(world):
    r = await world.client.post("/api/session", json={"role": "counter"})
    sid = r.json()["session_id"]
    r = await world.client.post("/api/session/end", json={"session_id": sid})
    assert r.status_code == 200
    await settle()
    assert sid not in world.store.sessions and world.armor.closed == 1
    r = await world.client.post("/api/session/end", json={"session_id": sid})
    assert r.status_code == 404


async def test_ended_call_with_a_hold_closes_after_the_owner_decides(world):
    world.armor.rules = {"process_exchange": "hold"}
    s = world.add_session()
    r = await call(world, s, "process_exchange", {"order_id": "1077", "new_item": "Elite", "amount": 170, "reason": "r"})
    world.gate.end_session(s)
    await settle()
    assert s.session_id in world.store.sessions and world.armor.closed == 0
    p = await world.gate.approve(r["decision"]["pending_id"], "Sam")
    await settle()
    assert p["status"] == "executed"
    assert s.session_id not in world.store.sessions and world.armor.closed == 1


async def test_idle_calls_expire_when_a_new_one_starts(world):
    old = world.add_session()
    old.last_seen -= world.gate.idle_s + 1
    fresh = world.add_session()
    r = await world.client.post("/api/session", json={"role": "counter"})
    assert r.status_code == 200
    await settle()
    assert old.session_id not in world.store.sessions
    assert fresh.session_id in world.store.sessions and r.json()["session_id"] in world.store.sessions
    assert world.armor.closed == 1


async def test_concurrent_calls_keep_check_and_report_in_one_turn():
    """Real ArmorIQSession over a recording SDK stub: a second call's check and a
    usage write wait until the first call's report and flush are shipped."""
    import asyncio
    import threading

    from counter_backend.armor import ArmorIQSession

    log: list[str] = []
    lock = threading.Lock()

    class SDK:
        def check(self, name, args, user_email=None):
            with lock:
                log.append(f"check {name}")
            return type("R", (), {"allowed": True, "action": "allow", "reason": "allow:x#0"})()

        def report(self, name, args, result, opts):
            with lock:
                log.append(f"report {name}")

        def flush_observability(self):
            with lock:
                log.append("flush")

        def record_generation(self, **kw):
            with lock:
                log.append("usage")

    s = ArmorIQSession(SDK(), "c@example.com")
    gate_a = asyncio.Event()

    async def call_a():
        async with s.turn() as t:
            await t.check("lookup_price", {})
            await gate_a.wait()  # the MCP call is slow
            t.report("lookup_price", {}, {"ok": 1})

    async def call_b():
        async with s.turn() as t:
            await t.check("get_order", {})
            t.report("get_order", {}, {"ok": 1})

    a = asyncio.create_task(call_a())
    await asyncio.sleep(0.05)
    b = asyncio.create_task(call_b())
    u = asyncio.create_task(s.record_generation("m", 10, 2))
    await asyncio.sleep(0.05)
    assert log == ["check counter__lookup_price"]  # b and the usage write wait
    gate_a.set()
    await asyncio.gather(a, b, u)
    for _ in range(50):
        if len(log) >= 7:
            break
        await asyncio.sleep(0.02)
    assert log[:3] == ["check counter__lookup_price", "report counter__lookup_price", "flush"]
    assert sorted(log[3:]) == sorted(["check counter__get_order", "report counter__get_order", "flush",
                                      "usage", "flush"])
    i = log.index("check counter__get_order")
    assert log[i + 1:i + 3] == ["report counter__get_order", "flush"]
