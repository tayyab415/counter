"""HTTP API tests: sessions, /api/tool, pending approve/reject, reset, static/health."""
import base64
from pathlib import Path

from conftest import FakeArmor, FakeArmorSession, FakeMCP, build_world, settle
from counter_backend.config import Settings
from counter_backend.main import create_app
from counter_backend.store import CounterSession

OWNER_HEADERS = {"X-Owner-Token": "tok-owner-1"}


async def owner_world(world):
    """Give the world an owner session + token."""
    return world.add_session(role="owner", owner_token="tok-owner-1")


# ---------------------------------------------------------------- sessions
async def test_counter_session(world):
    r = await world.client.post("/api/session", json={"role": "counter"})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "counter"
    assert body["identity_label"] == "counter@shop"
    assert "owner_token" not in body
    sid = body["session_id"]
    session = world.store.sessions[sid]
    assert session.role == "counter"
    assert session.identity.startswith("counter-") and session.identity.endswith("@example.com")
    assert len(session.identity.split("@")[0]) == len("counter-") + 8
    assert body["gemini"] == {"token": "fake-ephemeral-token", "model": "gemini-3.1-flash-live-preview"}
    assert len(body["tools"]) == 11
    assert {t["name"] for t in body["tools"]} == {
        "lookup_price", "get_order", "record_inspection", "notify_customer_on_file", "page_owner",
        "issue_payout", "issue_refund", "process_exchange", "match_competitor_quote",
        "waive_lock_check", "refund_to_account"}
    assert body["tools"][0]["parameters"]["type"] == "OBJECT"
    assert "Call the tool before you tell the customer" in body["system_prompt"]
    assert world.armor.sessions[-1].email == session.identity


async def test_two_counter_sessions_have_distinct_identities(world):
    a = (await world.client.post("/api/session", json={"role": "counter"})).json()
    b = (await world.client.post("/api/session", json={"role": "counter"})).json()
    assert a["session_id"] != b["session_id"]
    assert world.store.sessions[a["session_id"]].identity != world.store.sessions[b["session_id"]].identity


async def test_owner_session_password(world):
    r = await world.client.post("/api/session", json={"role": "owner", "password": "wrong"})
    assert r.status_code == 401
    r = await world.client.post("/api/session", json={"role": "owner"})
    assert r.status_code == 401
    r = await world.client.post("/api/session", json={"role": "owner", "password": "pw"})
    assert r.status_code == 200
    body = r.json()
    assert body["identity_label"] == "owner \u00b7 Sam"
    assert body["owner_token"]
    assert world.store.sessions[body["session_id"]].identity == world.settings.armor_login
    assert body["owner_token"] in world.store.owner_tokens


async def test_bad_role(world):
    r = await world.client.post("/api/session", json={"role": "admin"})
    assert r.status_code == 400


# ---------------------------------------------------------------- /api/tool over HTTP
async def test_tool_unknown_session(world):
    r = await world.client.post("/api/tool", json={"session_id": "ses-nope", "name": "get_order",
                                                   "args": {"order_id": "1042"}})
    assert r.status_code == 404


async def test_tool_refund_flow_over_http(world):
    s = world.add_session()
    r = await world.client.post("/api/tool", json={
        "session_id": s.session_id, "call_id": "c1", "name": "issue_refund",
        "args": {"order_id": "1101", "amount": 45, "reason": "doesn't fit"},
        "camera_on": True, "still": None})
    assert r.status_code == 200
    body = r.json()
    assert body["response"]["status"] == "refunded"
    assert body["decision"]["action"] == "allow"
    assert body["decision"]["session_id"] == s.session_id


async def test_tool_refund_to_account_blocked_posts_to_slack(world):
    world.armor.rules["refund_to_account"] = "block"
    s = world.add_session()
    r = await world.client.post("/api/tool", json={
        "session_id": s.session_id, "name": "refund_to_account",
        "args": {"order_id": "1101", "amount": 45, "account": "Mastercard ending 7741"},
        "camera_on": True})
    body = r.json()
    assert body["response"]["status"] == "blocked_by_policy"
    assert body["decision"]["layer"] == "ARMORIQ"
    await settle()
    assert len(world.slack.posted_blocks) == 1


# ---------------------------------------------------------------- pending + approve/reject
async def test_pending_requires_owner_token(world):
    await owner_world(world)
    assert (await world.client.get("/api/pending")).status_code == 401
    assert (await world.client.get("/api/pending", headers={"X-Owner-Token": "nope"})).status_code == 401
    r = await world.client.get("/api/pending", headers=OWNER_HEADERS)
    assert r.status_code == 200 and r.json() == []


async def test_approve_executes_stored_action_once(world):
    world.armor.rules["process_exchange"] = "hold"
    await owner_world(world)
    s = world.add_session()
    still = base64.b64encode(b"\xff\xd8\xff\xe0jpegdata").decode()
    r = await world.client.post("/api/tool", json={
        "session_id": s.session_id, "name": "process_exchange",
        "args": {"order_id": "1077", "new_item": "Xbox Elite", "amount": 170, "reason": "upgrade"},
        "camera_on": True, "still": still, "customer_said": "I'll pay the difference"})
    body = r.json()
    assert body["response"]["status"] == "held_for_approval"
    pid = body["decision"]["pending_id"]

    r = await world.client.get("/api/pending", headers=OWNER_HEADERS)
    pend = r.json()
    assert len(pend) == 1
    assert pend[0]["pending_id"] == pid
    assert pend[0]["has_still"] is True
    assert "still" not in pend[0]
    assert pend[0]["delegation_id"] == "deleg-01"

    r = await world.client.get(f"/api/pending/{pid}/still.jpg", headers=OWNER_HEADERS)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.content == b"\xff\xd8\xff\xe0jpegdata"

    r = await world.client.post(f"/api/pending/{pid}/approve", headers=OWNER_HEADERS)
    assert r.status_code == 200
    p = r.json()
    assert p["status"] == "executed"
    assert p["result"]["status"] == "exchanged" and p["result"]["charged"] == 170
    assert world.armor.approved == ["deleg-01"]
    executed = [c for c in world.mcp.calls if c[0] == "process_exchange"]
    assert len(executed) == 1
    # the re-check happened with the stored args
    assert ("process_exchange", {"order_id": "1077", "new_item": "Xbox Elite",
                                 "amount": 170, "reason": "upgrade"}) in [(t, a) for t, a, _ in world.armor.checks]

    # double approve -> 409, still exactly one execution
    r = await world.client.post(f"/api/pending/{pid}/approve", headers=OWNER_HEADERS)
    assert r.status_code == 409
    assert len([c for c in world.mcp.calls if c[0] == "process_exchange"]) == 1
    await settle()
    assert len(world.slack.updated) == 1 and world.slack.updated[0]["status"] == "executed"

    # the case card flipped
    assert s.case["kind"] == "exchange"
    assert s.case["outcome"]["status"] == "exchanged"


async def test_reject_marks_rejected_and_409_on_second(world):
    world.armor.rules["issue_payout"] = "hold"
    await owner_world(world)
    s = world.add_session()
    r = await world.client.post("/api/tool", json={
        "session_id": s.session_id, "name": "issue_payout",
        "args": {"customer_id": "C-311", "amount": 220, "reason": "trade-in"}, "camera_on": True})
    pid = r.json()["decision"]["pending_id"]
    r = await world.client.post(f"/api/pending/{pid}/reject", headers=OWNER_HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert world.armor.rejected and world.armor.rejected[0][0] == "deleg-01"
    r = await world.client.post(f"/api/pending/{pid}/reject", headers=OWNER_HEADERS)
    assert r.status_code == 409
    # nothing executed
    assert not any(c[0] == "issue_payout" for c in world.mcp.calls)


async def test_approve_unknown_pending_is_409(world):
    await owner_world(world)
    r = await world.client.post("/api/pending/pen-nope/approve", headers=OWNER_HEADERS)
    assert r.status_code == 409


async def test_approve_fails_when_recheck_does_not_allow(world):
    world.armor.rules["process_exchange"] = "hold"
    await owner_world(world)
    s = world.add_session()
    r = await world.client.post("/api/tool", json={
        "session_id": s.session_id, "name": "process_exchange",
        "args": {"order_id": "1077", "new_item": "Elite", "amount": 170, "reason": "u"}, "camera_on": True})
    pid = r.json()["decision"]["pending_id"]
    # ArmorIQ never approves this delegation: the re-check keeps saying hold
    world.armor.holds.clear()
    r = await world.client.post(f"/api/pending/{pid}/approve", headers=OWNER_HEADERS)
    assert r.status_code == 200
    p = r.json()
    assert p["status"] == "failed"
    assert "re-check did not allow" in p["detail"]
    assert not any(c[0] == "process_exchange" for c in world.mcp.calls)


async def test_pending_still_404_when_missing(world):
    await owner_world(world)
    r = await world.client.get("/api/pending/pen-whatever/still.jpg", headers=OWNER_HEADERS)
    assert r.status_code == 404


# ---------------------------------------------------------------- events auth
async def test_events_requires_owner_token_for_star(world):
    await owner_world(world)
    r = await world.client.get("/api/events", params={"session_id": "*"})
    assert r.status_code == 401
    r = await world.client.get("/api/events", params={"session_id": "*", "token": "bad"})
    assert r.status_code == 401


async def test_events_unknown_session_404(world):
    r = await world.client.get("/api/events", params={"session_id": "ses-nope"})
    assert r.status_code == 404


# ---------------------------------------------------------------- reset / health
async def test_reset(world):
    world.armor.rules["process_exchange"] = "hold"
    await owner_world(world)
    s = world.add_session()
    await world.client.post("/api/tool", json={
        "session_id": s.session_id, "name": "process_exchange",
        "args": {"order_id": "1077", "new_item": "Elite", "amount": 170, "reason": "u"}, "camera_on": True})
    await world.client.post("/api/tool", json={
        "session_id": s.session_id, "name": "issue_refund",
        "args": {"order_id": "1101", "amount": 45, "reason": "r"}, "camera_on": True})
    assert world.store.pending and world.store.refunded_orders == {"1101"}

    r = await world.client.post("/api/reset", headers=OWNER_HEADERS)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert world.mcp.reset_calls == 1
    assert world.store.pending == {}
    assert world.store.refunded_orders == set()
    assert world.store.sessions == {}
    # owner token was cleared with the sessions
    assert (await world.client.get("/api/pending", headers=OWNER_HEADERS)).status_code == 401


async def test_reset_needs_owner(world):
    r = await world.client.post("/api/reset")
    assert r.status_code == 401


async def test_healthz(world):
    r = await world.client.get("/api/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


# ---------------------------------------------------------------- no slack configured
async def test_gate_runs_without_slack():
    settings = Settings(armor_api_key="k", armor_login="o@x", owner_password="p", mcp_token="t",
                        web_dir=Path("/missing"))
    armor, mcp = FakeArmor(), FakeMCP()
    app = create_app(settings=settings, armor=armor, mcp=mcp, slack=None,
                     token_minter=lambda: {"token": "x", "model": "m"})
    gate, store = app.state.gate, app.state.store
    assert gate.slack is None
    s = CounterSession(session_id="ses-1", role="counter", identity="c@x", identity_label="counter@shop",
                       armor_session=FakeArmorSession(armor, "c@x"))
    store.sessions["ses-1"] = s
    armor.rules["match_competitor_quote"] = "block"
    r = await gate.handle_tool(s, "match_competitor_quote", {"competitor": "g", "amount": 420}, True)
    assert r["response"]["status"] == "blocked_by_policy"
    r = await gate.handle_tool(s, "get_order", {"order_id": "1042"}, True)
    assert r["response"]["order_id"] == "1042"


# ---------------------------------------------------------------- settings
def test_settings_web_path(tmp_path):
    sett = Settings(armor_api_key="k", armor_login="l", owner_password="p", mcp_token="t",
                    web_dir=tmp_path)
    assert sett.web_path == tmp_path
