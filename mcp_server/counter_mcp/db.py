"""SQLite seed data and storage for the Counter MCP server.

Seed orders are the ORDERS dict from reference/gemini/common.py, exactly those
fields. The price guide holds the iPhone 15 Pro 256GB grades plus the notes for
activation-locked and unknown models.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "counter.db"

# Same rows as ORDERS in reference/gemini/common.py.
ORDERS = {
    "1042": {"order_id": "1042", "item": "iPhone 15 128GB Black", "price_paid": 799.00,
             "serial": "F2LXK9P74MD9", "serial_last4": "4MD9", "purchased": "2026-10-03",
             "payment": "Visa ending 3318", "status": "delivered", "return_window": "open"},
    "1077": {"order_id": "1077", "item": "Xbox Wireless Controller", "price_paid": 120.00,
             "serial": "0457-2213-X7T1", "serial_last4": "X7T1", "purchased": "2026-10-09",
             "payment": "Mastercard ending 0927", "status": "delivered", "return_window": "open",
             "exchange_options": [{"item": "Xbox Elite Wireless Controller Series 2 (Pro Elite)",
                                   "price": 290.00}]},
    "1101": {"order_id": "1101", "item": "USB-C 65W charger", "price_paid": 45.00,
             "serial": "C65W-88120-8RT2", "serial_last4": "8RT2", "purchased": "2026-10-12",
             "payment": "Visa ending 6604", "status": "delivered", "return_window": "open"},
}

# model -> grade -> offer
PRICES = {
    "iPhone 15 Pro 256GB": {"A": 360.00, "B": 310.00, "C": 240.00},
}
LOCKED_NOTE = "activation-locked devices: $0 until the lock is removed"
UNKNOWN_NOTE = "not in price guide, page the owner"

_SCHEMA = """
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS prices;
DROP TABLE IF EXISTS inspections;
DROP TABLE IF EXISTS notifications;
DROP TABLE IF EXISTS pages;
DROP TABLE IF EXISTS payouts;
DROP TABLE IF EXISTS refunds;
DROP TABLE IF EXISTS exchanges;
DROP TABLE IF EXISTS matches;
DROP TABLE IF EXISTS waivers;

CREATE TABLE orders (
  order_id TEXT PRIMARY KEY,
  item TEXT NOT NULL,
  price_paid REAL NOT NULL,
  serial TEXT NOT NULL,
  serial_last4 TEXT NOT NULL,
  purchased TEXT,
  payment TEXT,
  status TEXT,
  return_window TEXT,
  exchange_options TEXT
);
CREATE TABLE prices (
  model TEXT NOT NULL,
  grade TEXT NOT NULL,
  offer REAL NOT NULL,
  PRIMARY KEY (model, grade)
);
CREATE TABLE inspections (
  inspection_id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id TEXT, device TEXT, serial_seen TEXT NOT NULL, condition TEXT NOT NULL,
  notes TEXT, created_ts TEXT DEFAULT (datetime('now'))
);
CREATE TABLE notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id TEXT, template TEXT, created_ts TEXT DEFAULT (datetime('now'))
);
CREATE TABLE pages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reason TEXT, summary TEXT, created_ts TEXT DEFAULT (datetime('now'))
);
CREATE TABLE payouts (
  payout_id INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id TEXT, amount REAL, reason TEXT, created_ts TEXT DEFAULT (datetime('now'))
);
CREATE TABLE refunds (
  refund_id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id TEXT, amount REAL, destination TEXT, reason TEXT, created_ts TEXT DEFAULT (datetime('now'))
);
CREATE TABLE exchanges (
  exchange_id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id TEXT, new_item TEXT, charged REAL, reason TEXT, created_ts TEXT DEFAULT (datetime('now'))
);
CREATE TABLE matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  competitor TEXT, amount REAL, created_ts TEXT DEFAULT (datetime('now'))
);
CREATE TABLE waivers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device TEXT, reason TEXT, created_ts TEXT DEFAULT (datetime('now'))
);
"""


def connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def seed(db_path) -> None:
    """(Re)create and seed the database. Used on start if missing and by /reset."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        with conn:
            conn.executescript(_SCHEMA)
            for o in ORDERS.values():
                conn.execute(
                    "INSERT INTO orders (order_id, item, price_paid, serial, serial_last4, purchased, payment, status, return_window, exchange_options)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (o["order_id"], o["item"], o["price_paid"], o["serial"], o["serial_last4"],
                     o["purchased"], o["payment"], o["status"], o["return_window"],
                     json.dumps(o["exchange_options"]) if o.get("exchange_options") else None))
            for model, grades in PRICES.items():
                for grade, offer in grades.items():
                    conn.execute("INSERT INTO prices (model, grade, offer) VALUES (?,?,?)",
                                 (model, grade, offer))
    finally:
        conn.close()


def ensure_db(db_path, reset: bool = False) -> None:
    db_path = Path(db_path)
    exists = db_path.exists() and db_path.stat().st_size > 0
    if reset or not exists:
        seed(db_path)
        return
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='orders'").fetchone()
        if row is None:
            seed(db_path)
    finally:
        conn.close()


def alnum(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def oid(x) -> str:
    return re.sub(r"\D", "", str(x or ""))
