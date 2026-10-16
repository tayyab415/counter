"""ArmorIQ gate: one session + plan per identity, checks, delegation approve/reject.

The SDK is sync; every call runs in a thread. `mode="sdk"`, policy variant B is
checked by the org (probe-org / counter-policy).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger("counter.armor")


@dataclass
class Check:
    allowed: bool
    action: str  # "allow" | "hold" | "block"
    reason: str
    delegation_id: str | None = None
    matched_policy: str | None = None


class ArmorIQSession:
    """One ArmorIQ session for one identity, with its plan already started."""

    def __init__(self, session, email: str, plan_id: str | None = None):
        self._session = session
        self.email = email
        self.plan_id = plan_id

    async def check(self, tool: str, args: dict, user_email: str | None = None) -> Check:
        name = tool if tool.startswith("counter__") else f"counter__{tool}"
        r = await asyncio.to_thread(self._session.check, name, dict(args or {}),
                                    user_email=user_email or self.email)
        return Check(allowed=bool(r.allowed), action=str(r.action), reason=str(r.reason or ""),
                     delegation_id=getattr(r, "delegation_id", None),
                     matched_policy=getattr(r, "matched_policy", None))


class ArmorIQLayer:
    """Creates ArmorIQ sessions and calls the delegation endpoints."""

    def __init__(self, api_key: str, login: str, backend: str = "https://api.armoriq.ai"):
        if not api_key:
            raise RuntimeError("ARMORIQ_API_KEY is not set (add it to ~/.config/counter/.env)")
        if not login:
            raise RuntimeError("ARMORIQ_LOGIN is not set (add it to ~/.config/counter/.env)")
        from armoriq_sdk import ArmorIQClient

        self._client = ArmorIQClient(api_key=api_key)
        self._api_key = api_key
        self.login = login
        self.backend = backend.rstrip("/")

    async def start_session(self, email: str) -> ArmorIQSession:
        return await asyncio.to_thread(self._start, email)

    def _start(self, email: str) -> ArmorIQSession:
        from armoriq_sdk.session import ObservabilityConfig, SessionOptions

        from .tools import PLAN_ARGS, TOOL_NAMES

        opts = SessionOptions(mode="sdk", default_mcp_name="counter", validity_seconds=3600)
        try:
            opts.observability = ObservabilityConfig(enabled=False)
        except Exception:  # pragma: no cover - SDK shape guard
            pass
        plan = [{"name": f"counter__{t}", "args": PLAN_ARGS[t]} for t in TOOL_NAMES]
        last: Exception | None = None
        for attempt in range(2):
            try:
                session = self._client.for_user(email).start_session(opts)
                tok = session.start_plan(plan, goal="counter front desk")
                return ArmorIQSession(session, email, plan_id=getattr(tok, "plan_id", None))
            except Exception as e:  # retry once: a stale rejection can fail the first plan
                last = e
                log.warning("ArmorIQ start_plan attempt %d failed: %s", attempt + 1, type(e).__name__)
                time.sleep(0.75 * (attempt + 1))
        assert last is not None
        raise last

    async def approve_delegation(self, delegation_id: str) -> tuple[int, object]:
        return await self._delegation("approve", delegation_id, {"approverRole": "org_admin"})

    async def reject_delegation(self, delegation_id: str, reason: str) -> tuple[int, object]:
        return await self._delegation("reject", delegation_id, {"rejectionReason": reason})

    async def _delegation(self, action: str, delegation_id: str, extra: dict) -> tuple[int, object]:
        body = {"delegationId": delegation_id, "approverEmail": self.login, **extra}
        headers = {"X-API-Key": self._api_key, "x-user-email": self.login}
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{self.backend}/delegation/{action}", json=body, headers=headers)
        try:
            parsed: object = r.json()
        except Exception:
            parsed = r.text[:500]
        return r.status_code, parsed
