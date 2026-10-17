"""In-memory state: sessions, decisions, pending actions, refund registry and
the SSE fan-out."""
from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


@dataclass
class CounterSession:
    session_id: str
    role: str                     # "counter" | "owner"
    identity: str                 # ArmorIQ email
    identity_label: str
    owner_token: str | None = None
    armor_session: object | None = None
    created_ts: str = field(default_factory=now_iso)
    last_seen: float = field(default_factory=time.monotonic)  # last tool call or usage
    ended: bool = False           # the page said the call is over, or it went idle
    # order_id -> serial_seen, recorded by this session's record_inspection
    inspections: dict[str, str] = field(default_factory=dict)
    case: dict = field(default_factory=lambda: {"kind": None, "order": None, "inspection": None, "outcome": None})


def pending_view(p: dict) -> dict:
    """PendingAction without the still bytes."""
    out = {k: v for k, v in p.items() if k != "still"}
    out["has_still"] = bool(p.get("still"))
    return out


@dataclass
class Store:
    sessions: dict[str, CounterSession] = field(default_factory=dict)
    pending: dict[str, dict] = field(default_factory=dict)
    decisions: list[dict] = field(default_factory=list)
    refunded_orders: set[str] = field(default_factory=set)
    owner_tokens: set[str] = field(default_factory=set)
    # order_id -> serial_last4 (server side only; the model never sees this value)
    serial_last4: dict[str, str | None] = field(default_factory=dict)
    _subs: dict[str, set[asyncio.Queue]] = field(default_factory=dict)

    # ---- event fan-out -------------------------------------------------
    def subscribe(self, key: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.setdefault(key, set()).add(q)
        return q

    def unsubscribe(self, key: str, q: asyncio.Queue) -> None:
        subs = self._subs.get(key)
        if subs:
            subs.discard(q)
            if not subs:
                self._subs.pop(key, None)

    def publish(self, session_id: str | None, etype: str, data: dict) -> None:
        payload = {"type": etype, "data": data}
        targets: list[str] = ["*"]
        if session_id:
            targets.append(session_id)
        for key in targets:
            for q in list(self._subs.get(key, ())):
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    pass

    def add_decision(self, decision: dict) -> None:
        self.decisions.append(decision)
        if len(self.decisions) > 500:
            del self.decisions[: len(self.decisions) - 500]
