"""Case plans: the tools ArmorIQ allows for the case the customer opened.

The ArmorIQ SDK blocks any call to a tool that is not in the session's current
plan (`tool-not-in-plan`). A conversation starts on the intake plan. When the
model calls `open_case`, the backend starts a new plan on the same ArmorIQ
session with the fixed tool list for that case type.

Only tool names live here. Nothing depends on the policy format or rule ids.
"""
from __future__ import annotations

from .store import CounterSession, now_iso
from .tools import OPEN_CASE, OPEN_CASE_DECL, PLAN_ARGS, TOOL_NAMES  # noqa: F401

INTAKE = "intake"
CASE_TYPES = ("return", "exchange", "trade_in")
NEEDS_ORDER = {"return", "exchange"}

PLANS: dict[str, list[str]] = {
    INTAKE: ["lookup_price", "get_order", "page_owner"],
    "return": ["get_order", "record_inspection", "issue_refund", "notify_customer_on_file", "page_owner",
               "refund_to_account"],
    "exchange": ["get_order", "record_inspection", "process_exchange", "notify_customer_on_file", "page_owner"],
    "trade_in": ["lookup_price", "record_inspection", "issue_payout", "match_competitor_quote",
                 "waive_lock_check", "page_owner"],
}
ALL_TOOLS = "all"  # the owner keeps every tool

# A case closes when one of these runs, or when a hold on one is resolved.
CLOSING_TOOLS = {"issue_refund", "process_exchange", "issue_payout"}
# In a return or exchange case these must name the case's order.
ORDER_BOUND_TOOLS = {"issue_refund", "process_exchange"}

LABEL = {INTAKE: "intake", "return": "return", "exchange": "exchange", "trade_in": "trade-in"}


def normalize_type(x) -> str:
    return str(x or "").strip().lower().replace("-", "_").replace(" ", "_")


def goal(case_type: str, order_id: str | None) -> str:
    if case_type == INTAKE:
        return "counter intake"
    return f"{case_type} order {order_id}" if order_id else case_type


def plan_calls(tools: list[str], order_id: str | None = None) -> list[dict]:
    """The plan in the SDK's shape. ArmorIQ does not bind args, so these are samples;
    the case's order id is filled in so the console shows it."""
    out = []
    for t in tools:
        args = dict(PLAN_ARGS[t])
        if order_id and "order_id" in args:
            args["order_id"] = order_id
        out.append({"name": f"counter__{t}", "args": args})
    return out


def new_plan(case_type: str, order_id: str | None, tools: list[str], plan_id: str | None = None) -> dict:
    return {"case_type": case_type, "order_id": order_id or None, "tools": list(tools),
            "status": "open", "goal": goal(case_type, order_id), "plan_id": plan_id,
            "opened_ts": now_iso(), "closed_ts": None}


def intake_tools(role: str) -> list[str]:
    return list(TOOL_NAMES) if role == "owner" else list(PLANS[INTAKE])


def case_tools(role: str, case_type: str) -> list[str]:
    return list(TOOL_NAMES) if role == "owner" else list(PLANS[case_type])


def describe_case(plan: dict | None) -> str:
    """"return for order 1101", "trade-in", "intake"."""
    if not plan:
        return "intake"
    label = LABEL.get(plan["case_type"], plan["case_type"])
    return f"{label} for order {plan['order_id']}" if plan.get("order_id") else label


def in_case(session: CounterSession) -> bool:
    """A case (open or closed) owns the current plan."""
    return bool(session.plan) and session.plan["case_type"] != INTAKE


def case_open(session: CounterSession) -> bool:
    return in_case(session) and session.plan["status"] == "open"


def plan_event(session: CounterSession) -> dict:
    """SSE `plan` event: what the web UI needs for "Plan: return order 1101"."""
    p = session.plan or {}
    case_type = p.get("case_type", INTAKE)
    label = "Plan: intake" if case_type == INTAKE else f"Plan: {LABEL.get(case_type, case_type)}" + (
        f" order {p['order_id']}" if p.get("order_id") else "")
    return {"session_id": session.session_id, "case_type": case_type, "order_id": p.get("order_id"),
            "status": p.get("status", "open"), "tools": p.get("tools", []), "goal": p.get("goal"),
            "plan_id": p.get("plan_id"), "label": label, "opened_ts": p.get("opened_ts"),
            "closed_ts": p.get("closed_ts")}


def order_refusal(session: CounterSession, name: str, order_id: str) -> str | None:
    """Shop rule: a money tool in a return or exchange case must use the case's order."""
    p = session.plan
    if name not in ORDER_BOUND_TOOLS or not in_case(session) or p["case_type"] not in NEEDS_ORDER:
        return None
    if p.get("order_id") and order_id != p["order_id"]:
        return f"this case is the {describe_case(p)}, not order {order_id or '(none)'}"
    return None
