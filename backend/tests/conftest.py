"""Test doubles: fake ArmorIQ layer, fake MCP server, fake Slack bridge."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from counter_backend.armor import Check
from counter_backend.config import Settings
from counter_backend.main import create_app
from counter_backend.store import CounterSession, new_id

ORDERS = {
    "1042": {"order_id": "1042", "item": "iPhone 15 128GB Black", "price_paid": 799.0,
             "serial": "F2LXK9P74MD9", "serial_last4": "4MD9", "purchased": "2026-10-03",
             "payment": "Visa ending 3318", "status": "delivered", "return_window": "open"},
    "1077": {"order_id": "1077", "item": "Xbox Wireless Controller", "price_paid": 120.0,
             "serial": "0457-2213-X7T1", "serial_last4": "X7T1", "purchased": "2026-10-09",
             "payment": "Mastercard ending 0927", "status": "delivered", "return_window": "open",
             "exchange_options": [{"item": "Xbox Elite Wireless Controller Series 2 (Pro Elite)",
                                   "price": 290.0}]},
    "1101": {"order_id": "1101", "item": "USB-C 65W charger", "price_paid": 45.0,
             "serial": "C65W-88120-8RT2", "serial_last4": "8RT2", "purchased": "2026-10-12",
             "payment": "Visa ending 6604", "status": "delivered", "return_window": "open"},
}


class FakeArmorSession:
    def __init__(self, layer: "FakeArmor", email: str, tools: list[str] | None = None):
        self.layer = layer
        self.email = email
        self.plan: set[str] | None = set(tools) if tools else None   # None: every tool in plan
        self.plan_id = "plan-0"

    async def start_plan(self, tools: list[str], goal: str, order_id: str | None = None) -> str:
        if self.layer.raise_on_plan:
            raise RuntimeError("armoriq is down")
        self.plan = set(tools)
        self.layer.plans.append((self.email, list(tools), goal))
        self.plan_id = f"plan-{len(self.layer.plans)}"
        return self.plan_id

    async def check(self, tool: str, args: dict, user_email: str | None = None) -> Check:
        if self.layer.raise_on_check:
            raise RuntimeError("armoriq is down")
        self.layer.checks.append((tool, dict(args), user_email or self.email))
        tool = tool.removeprefix("counter__")
        if self.plan is not None and tool not in self.plan:   # the SDK's local intent check
            return Check(allowed=False, action="block",
                         reason=f"tool-not-in-plan: 'counter__{tool}' was not declared in the captured plan")
        action = self.layer.rules.get(tool, self.layer.default_action)
        if tool in self.layer.approved_tools:
            return Check(allowed=True, action="allow", reason="approved_delegation")
        if action == "hold":
            did = f"deleg-{len(self.layer.holds) + 1:02d}"
            self.layer.holds[did] = tool
            return Check(allowed=False, action="hold", reason="amount_requires_approval", delegation_id=did)
        if action == "block":
            return Check(allowed=False, action="block", reason="tool_not_in_allowlist")
        return Check(allowed=True, action="allow", reason="allow_with_logging")

    async def report(self, tool, args, result, ok=True, error=None, ms=None, delegated=False):
        self.layer.reports.append((tool, dict(args or {}), ok, delegated))

    async def flush(self):
        self.layer.flushes += 1

    async def record_generation(self, model, input_tokens, output_tokens, cached_tokens=0):
        self.layer.usage.append((model, input_tokens, output_tokens, cached_tokens))

    async def close(self):
        self.layer.closed += 1


class FakeArmor:
    def __init__(self):
        self.sessions: list[FakeArmorSession] = []
        self.rules: dict[str, str] = {}
        self.default_action = "allow"
        self.raise_on_check = False
        self.raise_on_plan = False
        self.plans: list[tuple] = []             # (email, tools, goal) for every start_plan after the first
        self.started: list[tuple] = []           # (email, tools, goal) for start_session
        self.checks: list[tuple] = []
        self.holds: dict[str, str] = {}          # delegation_id -> tool
        self.approved_tools: set[str] = set()
        self.approved: list[str] = []
        self.rejected: list[tuple[str, str]] = []
        self.reports: list[tuple] = []           # (tool, args, ok, delegated) sent to the console
        self.flushes = 0
        self.usage: list[tuple] = []
        self.closed = 0

    async def start_session(self, email: str, tools: list[str] | None = None,
                            goal: str = "counter front desk") -> FakeArmorSession:
        self.started.append((email, list(tools or []), goal))
        s = FakeArmorSession(self, email, tools)
        self.sessions.append(s)
        return s

    async def approve_delegation(self, delegation_id: str) -> tuple[int, object]:
        self.approved.append(delegation_id)
        tool = self.holds.get(delegation_id)
        if tool:
            self.approved_tools.add(tool)
        return 200, {"status": "approved"}

    async def reject_delegation(self, delegation_id: str, reason: str) -> tuple[int, object]:
        self.rejected.append((delegation_id, reason))
        return 200, {"status": "rejected"}


class FakeMCP:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.fail = False
        self.refund_n = 0
        self.reset_calls = 0

    async def call_tool(self, name: str, args: dict) -> dict:
        if self.fail:
            raise RuntimeError("mcp is down")
        self.calls.append((name, dict(args)))
        args = dict(args or {})
        if name == "get_order":
            order = ORDERS.get(str(args.get("order_id", "")))
            return dict(order) if order else {"error": "order not found"}
        if name == "lookup_price":
            return {"model": args.get("model"), "condition": args.get("condition"), "offer": 310.0,
                    "note": "grade B offer; activation lock must be off"}
        if name == "record_inspection":
            n = len([c for c in self.calls if c[0] == "record_inspection"])
            return {"status": "recorded", "inspection_id": f"insp-{n}",
                    "order_id": str(args.get("order_id")) or None,
                    "serial_seen": args.get("serial_seen"), "condition": args.get("condition")}
        if name == "notify_customer_on_file":
            return {"status": "sent", "to": f"contact on order {args.get('order_id')}",
                    "template": args.get("template")}
        if name == "page_owner":
            return {"status": "paged", "eta": "the owner will answer on their phone"}
        if name == "issue_payout":
            return {"status": "paid", "amount": args.get("amount"), "customer_id": args.get("customer_id"),
                    "payout_id": "pay-1"}
        if name == "issue_refund":
            self.refund_n += 1
            order = ORDERS.get(str(args.get("order_id")), {})
            return {"status": "refunded", "amount": args.get("amount"),
                    "to": order.get("payment", "original payment"), "refund_id": f"ref-{self.refund_n}"}
        if name == "process_exchange":
            return {"status": "exchanged", "order_id": str(args.get("order_id")),
                    "new_item": args.get("new_item"), "charged": args.get("amount"), "exchange_id": "exc-1"}
        if name == "match_competitor_quote":
            return {"status": "matched", "competitor": args.get("competitor"), "new_offer": args.get("amount")}
        if name == "waive_lock_check":
            return {"status": "waived", "device": args.get("device")}
        if name == "refund_to_account":
            self.refund_n += 1
            return {"status": "refunded", "amount": args.get("amount"), "to": args.get("account"),
                    "refund_id": f"ref-{self.refund_n}"}
        raise AssertionError(f"fake mcp got unknown tool {name}")

    async def reset(self) -> bool:
        self.reset_calls += 1
        self.calls.clear()
        return True


class FakeSlack:
    def __init__(self):
        self.posted_holds: list[tuple] = []
        self.posted_blocks: list[dict] = []
        self.updated: list[dict] = []

    async def post_hold(self, pending: dict, still_jpeg=None, customer_said=None) -> None:
        self.posted_holds.append((dict(pending), still_jpeg, customer_said))

    async def post_block(self, decision: dict) -> None:
        self.posted_blocks.append(dict(decision))

    async def update_hold(self, pending: dict) -> None:
        self.updated.append(dict(pending))


@dataclass
class World:
    settings: Settings
    store: object
    gate: object
    armor: FakeArmor
    mcp: FakeMCP
    slack: FakeSlack
    app: object
    client: httpx.AsyncClient

    def add_session(self, role: str = "counter", identity: str | None = None,
                    session_id: str | None = None, owner_token: str | None = None) -> CounterSession:
        sid = session_id or new_id("ses")
        armor_session = FakeArmorSession(self.armor, identity or "someone@example.com")
        self.armor.sessions.append(armor_session)
        s = CounterSession(session_id=sid, role=role,
                           identity=identity or ("owner@example.com" if role == "owner" else "counter-00000000@example.com"),
                           identity_label="owner \u00b7 Sam" if role == "owner" else "counter@shop",
                           owner_token=owner_token, armor_session=armor_session)
        self.store.sessions[sid] = s
        if owner_token:
            self.store.owner_tokens.add(owner_token)
        return s


def build_world(tmp_path: Path | None = None, settings: Settings | None = None, slack=None) -> World:
    settings = settings or Settings(armor_api_key="test-key", armor_login="owner@example.com",
                                    owner_password="pw", mcp_token="mcp-token", gemini_api_key="g",
                                    web_dir=Path("/definitely/missing-web"))
    armor = FakeArmor()
    mcp = FakeMCP()
    slack = slack if slack is not None else FakeSlack()
    app = create_app(settings=settings, armor=armor, mcp=mcp, slack=slack,
                     token_minter=lambda: _fake_token())
    transport = ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return World(settings=settings, store=app.state.store, gate=app.state.gate, armor=armor, mcp=mcp,
                 slack=slack, app=app, client=client)


async def _fake_token() -> dict:
    return {"token": "fake-ephemeral-token", "model": "gemini-3.1-flash-live-preview"}


@pytest.fixture
async def world(tmp_path):
    w = build_world(tmp_path)
    yield w
    await w.client.aclose()


async def settle() -> None:
    """Let fire-and-forget slack tasks run."""
    for _ in range(5):
        await asyncio.sleep(0)
