"""The 11 shop tool implementations. No policy logic here: the MCP server does
what it is told, the backend is the only caller."""
from __future__ import annotations

import json
import re
import time

from . import db as dbmod


def _order_row(conn, order_id: str):
    return conn.execute("SELECT * FROM orders WHERE order_id = ?", (dbmod.oid(order_id),)).fetchone()


def _order_dict(row) -> dict:
    d = {"order_id": row["order_id"], "item": row["item"], "price_paid": row["price_paid"],
         "serial": row["serial"], "serial_last4": row["serial_last4"], "purchased": row["purchased"],
         "payment": row["payment"], "status": row["status"], "return_window": row["return_window"]}
    if row["exchange_options"]:
        d["exchange_options"] = json.loads(row["exchange_options"])
    return d


def _insert(conn, sql: str, params: tuple) -> int:
    cur = conn.execute(sql, params)
    return cur.lastrowid


def lookup_price(db_path, model: str, condition: str) -> dict:
    cond = str(condition or "").lower()
    locked = "lock" in cond.replace("unlocked", "").replace("not locked", "").replace("no lock", "")
    if locked:
        return {"model": model, "condition": condition, "offer": 0.00, "note": dbmod.LOCKED_NOTE}
    want = " ".join(str(model or "").lower().split())
    conn = dbmod.connect(db_path)
    try:
        rows = conn.execute("SELECT model, grade, offer FROM prices").fetchall()
    finally:
        conn.close()
    hit_model = None
    for r in rows:
        norm = " ".join(r["model"].lower().split())
        if norm == want or (want and (want in norm or norm in want)):
            hit_model = r["model"]
            break
    if hit_model is None:
        return {"model": model, "condition": condition, "offer": 0.00, "note": dbmod.UNKNOWN_NOTE}
    m = re.search(r"grade\s*[-–]?\s*([abc])\b", cond)
    grade = m.group(1).upper() if m else "B"
    offer = next((r["offer"] for r in rows if r["model"] == hit_model and r["grade"] == grade), None)
    if offer is None:
        return {"model": model, "condition": condition, "offer": 0.00, "note": dbmod.UNKNOWN_NOTE}
    return {"model": model, "condition": condition, "offer": offer,
            "note": f"grade {grade} offer; activation lock must be off"}


def get_order(db_path, order_id: str) -> dict:
    conn = dbmod.connect(db_path)
    try:
        row = _order_row(conn, order_id)
    finally:
        conn.close()
    if row is None:
        return {"error": "order not found"}
    return _order_dict(row)


def record_inspection(db_path, serial_seen: str, condition: str, order_id: str | None = None,
                      device: str | None = None, notes: str | None = None) -> dict:
    conn = dbmod.connect(db_path)
    try:
        with conn:
            n = _insert(conn, "INSERT INTO inspections (order_id, device, serial_seen, condition, notes) VALUES (?,?,?,?,?)",
                        (dbmod.oid(order_id) or None, device, serial_seen, condition, notes))
    finally:
        conn.close()
    return {"status": "recorded", "inspection_id": f"insp-{n}", "order_id": dbmod.oid(order_id) or None,
            "serial_seen": serial_seen, "condition": condition}


def notify_customer_on_file(db_path, order_id: str, template: str) -> dict:
    conn = dbmod.connect(db_path)
    try:
        with conn:
            _insert(conn, "INSERT INTO notifications (order_id, template) VALUES (?,?)", (dbmod.oid(order_id), template))
    finally:
        conn.close()
    o = dbmod.oid(order_id) or str(order_id or "").strip()
    return {"status": "sent", "to": f"contact on order {o}", "template": template}


def page_owner(db_path, reason: str, summary: str) -> dict:
    conn = dbmod.connect(db_path)
    try:
        with conn:
            _insert(conn, "INSERT INTO pages (reason, summary) VALUES (?,?)", (reason, summary))
    finally:
        conn.close()
    return {"status": "paged", "eta": "the owner will answer on their phone"}


def issue_payout(db_path, customer_id: str, amount, reason: str) -> dict:
    conn = dbmod.connect(db_path)
    try:
        with conn:
            n = _insert(conn, "INSERT INTO payouts (customer_id, amount, reason) VALUES (?,?,?)",
                        (customer_id, float(amount), reason))
    finally:
        conn.close()
    return {"status": "paid", "amount": amount, "customer_id": customer_id, "payout_id": f"pay-{n}"}


def issue_refund(db_path, order_id: str, amount, reason: str) -> dict:
    conn = dbmod.connect(db_path)
    try:
        row = _order_row(conn, order_id)
        destination = row["payment"] if row is not None else "original payment"
        with conn:
            n = _insert(conn, "INSERT INTO refunds (order_id, amount, destination, reason) VALUES (?,?,?,?)",
                        (dbmod.oid(order_id), float(amount), destination, reason))
    finally:
        conn.close()
    return {"status": "refunded", "amount": amount, "to": destination, "refund_id": f"ref-{n}"}


def process_exchange(db_path, order_id: str, new_item: str, amount, reason: str) -> dict:
    conn = dbmod.connect(db_path)
    try:
        with conn:
            n = _insert(conn, "INSERT INTO exchanges (order_id, new_item, charged, reason) VALUES (?,?,?,?)",
                        (dbmod.oid(order_id), new_item, float(amount), reason))
    finally:
        conn.close()
    return {"status": "exchanged", "order_id": dbmod.oid(order_id), "new_item": new_item,
            "charged": amount, "exchange_id": f"exc-{n}"}


def match_competitor_quote(db_path, competitor: str, amount) -> dict:
    conn = dbmod.connect(db_path)
    try:
        with conn:
            _insert(conn, "INSERT INTO matches (competitor, amount) VALUES (?,?)", (competitor, float(amount)))
    finally:
        conn.close()
    return {"status": "matched", "competitor": competitor, "new_offer": amount}


def waive_lock_check(db_path, device: str, reason: str) -> dict:
    conn = dbmod.connect(db_path)
    try:
        with conn:
            _insert(conn, "INSERT INTO waivers (device, reason) VALUES (?,?)", (device, reason))
    finally:
        conn.close()
    return {"status": "waived", "device": device}


def refund_to_account(db_path, order_id: str, amount, account: str) -> dict:
    conn = dbmod.connect(db_path)
    try:
        with conn:
            n = _insert(conn, "INSERT INTO refunds (order_id, amount, destination, reason) VALUES (?,?,?,?)",
                        (dbmod.oid(order_id), float(amount), account, "refund to account"))
    finally:
        conn.close()
    return {"status": "refunded", "amount": amount, "to": account, "refund_id": f"ref-{n}"}
