"""Shop rules: our own code, in front of ArmorIQ."""
from __future__ import annotations

import re

MONEY_TOOLS = {"issue_payout", "issue_refund", "process_exchange", "match_competitor_quote", "refund_to_account"}
SERIAL_TOOLS = {"issue_refund", "process_exchange"}
CAMERA_TOOLS = {"record_inspection", "issue_refund", "process_exchange", "issue_payout"}


def num(x):
    try:
        if isinstance(x, bool):
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def alnum(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def oid(x) -> str:
    return re.sub(r"\D", "", str(x or ""))


def shop_rule_refusal(store, session, name: str, args: dict, camera_on: bool) -> str | None:
    """First refusal reason for this call, or None. Order matters: amount, serial,
    already refunded, camera, pending hold."""
    args = args or {}
    if name in MONEY_TOOLS:
        amt = num(args.get("amount"))
        if amt is None or amt <= 0:
            return "amount must be a positive number"
    if name in SERIAL_TOOLS:
        order = oid(args.get("order_id"))
        seen = session.inspections.get(order) if order else None
        last4 = store.serial_last4.get(order) if order else None
        if last4 and seen is not None and not alnum(seen).endswith(alnum(last4)):
            return "serial on camera doesn't match the order"
    if name == "issue_refund":
        order = oid(args.get("order_id"))
        if order and order in store.refunded_orders:
            return "this order was already refunded"
    if name in CAMERA_TOOLS and not camera_on:
        return "camera is off; I need to see the item"
    for p in store.pending.values():
        if p["session_id"] == session.session_id and p["tool"] == name and p["status"] == "waiting":
            return "waiting for the owner on an earlier request"
    return None
