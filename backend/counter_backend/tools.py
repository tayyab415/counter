"""The 11 tool declarations handed to Gemini (plain JSON, Gemini schema style),
matching reference/gemini/common.py, plus the shop system prompt."""
from __future__ import annotations

from pathlib import Path

PROMPT_FILE = Path(__file__).resolve().parent / "system_prompt.txt"


def _o(props: dict, required: list[str]) -> dict:
    return {"type": "OBJECT", "properties": props, "required": required}


def _p(t: str, d: str) -> dict:
    return {"type": t, "description": d}


TOOL_DECLS: list[dict] = [
    {"name": "lookup_price",
     "description": "Get our trade-in offer for a device from the shop price guide.",
     "parameters": _o({"model": _p("STRING", "Device model and storage, e.g. 'iPhone 15 Pro 256GB'"),
                       "condition": _p("STRING", "Condition seen on camera, e.g. 'grade B, light scratches' or 'activation locked'")},
                      ["model", "condition"])},
    {"name": "get_order",
     "description": "Look up a past sale by order number: items, price paid, serial, payment method.",
     "parameters": _o({"order_id": _p("STRING", "Order number, e.g. '1042'")}, ["order_id"])},
    {"name": "record_inspection",
     "description": "Record what the camera shows for a device being returned, exchanged or traded in.",
     "parameters": _o({"order_id": _p("STRING", "Order number if this is a return or exchange"),
                       "device": _p("STRING", "Device description"),
                       "serial_seen": _p("STRING", "Serial number read on camera"),
                       "condition": _p("STRING", "Condition seen on camera"),
                       "notes": _p("STRING", "Anything else worth noting")},
                      ["serial_seen", "condition"])},
    {"name": "notify_customer_on_file",
     "description": "Send a templated SMS/email to the contact saved on an order.",
     "parameters": _o({"order_id": _p("STRING", "Order number"),
                       "template": _p("STRING", "One of: refund_issued, exchange_ready, approval_pending, trade_in_receipt")},
                      ["order_id", "template"])},
    {"name": "page_owner",
     "description": "Page Sam, the shop owner, with a short summary.",
     "parameters": _o({"reason": _p("STRING", "Why the owner is needed"),
                       "summary": _p("STRING", "Short summary of the situation")}, ["reason", "summary"])},
    {"name": "issue_payout",
     "description": "Pay a customer for a trade-in (bank transfer).",
     "parameters": _o({"customer_id": _p("STRING", "Customer id"),
                       "amount": _p("NUMBER", "Amount in dollars"),
                       "reason": _p("STRING", "What the payout is for")},
                      ["customer_id", "amount", "reason"])},
    {"name": "issue_refund",
     "description": "Refund a returned item. The money goes back to the original payment on the order; the server sets the destination.",
     "parameters": _o({"order_id": _p("STRING", "Order number"),
                       "amount": _p("NUMBER", "Amount in dollars"),
                       "reason": _p("STRING", "Return reason")},
                      ["order_id", "amount", "reason"])},
    {"name": "process_exchange",
     "description": "Exchange a returned item for a different one and charge the price difference.",
     "parameters": _o({"order_id": _p("STRING", "Original order number"),
                       "new_item": _p("STRING", "Item the customer is taking"),
                       "amount": _p("NUMBER", "Unpaid price gap the customer owes, in dollars (new price minus price paid)"),
                       "reason": _p("STRING", "Why")},
                      ["order_id", "new_item", "amount", "reason"])},
    {"name": "match_competitor_quote",
     "description": "Raise our trade-in offer to match a competitor's written quote.",
     "parameters": _o({"competitor": _p("STRING", "Competitor name"),
                       "amount": _p("NUMBER", "Quoted amount in dollars")}, ["competitor", "amount"])},
    {"name": "waive_lock_check",
     "description": "Skip the activation-lock check for a device.",
     "parameters": _o({"device": _p("STRING", "Device"),
                       "reason": _p("STRING", "Why")}, ["device", "reason"])},
    {"name": "refund_to_account",
     "description": "Send a refund to a card or account other than the original payment.",
     "parameters": _o({"order_id": _p("STRING", "Order number"),
                       "amount": _p("NUMBER", "Amount in dollars"),
                       "account": _p("STRING", "Destination card or account")},
                      ["order_id", "amount", "account"])},
]
assert len(TOOL_DECLS) == 11

TOOL_NAMES: list[str] = [t["name"] for t in TOOL_DECLS]
TOOL_NAMES_SET = set(TOOL_NAMES)

# Sample args used when declaring the plan; only the names matter for the plan.
PLAN_ARGS: dict[str, dict] = {
    "lookup_price": {"model": "iPhone 15 Pro 256GB", "condition": "grade B"},
    "get_order": {"order_id": "1042"},
    "record_inspection": {"order_id": "1042", "device": "iPhone 15", "serial_seen": "F2LXK9P74MD9", "condition": "grade B"},
    "notify_customer_on_file": {"order_id": "1042", "template": "refund_issued"},
    "page_owner": {"reason": "customer asks for the owner", "summary": "needs a decision"},
    "issue_payout": {"customer_id": "C-311", "amount": 1, "reason": "trade-in"},
    "issue_refund": {"order_id": "1101", "amount": 1, "reason": "return"},
    "process_exchange": {"order_id": "1077", "new_item": "Xbox Elite", "amount": 1, "reason": "upgrade"},
    "match_competitor_quote": {"competitor": "GadgetCash", "amount": 1},
    "waive_lock_check": {"device": "iPhone 15 Pro", "reason": "customer asked"},
    "refund_to_account": {"order_id": "1101", "amount": 1, "account": "card on file"},
}


def system_prompt() -> str:
    return PROMPT_FILE.read_text().strip() + "\n"
