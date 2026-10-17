"""ArmorIQ gate: one session + plan per identity, checks, delegation approve/reject.

The SDK is sync; every call runs in a thread. `mode="sdk"`, policy `counter-policy`
(armor.io/v2, `deploy/armoriq/counter-policy.v2.json`) in the ArmorTools workspace.
Telemetry is on: each customer conversation is one session in the console at
tools.armoriq.ai, with every check and every tool run on it.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
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

    def __init__(self, session, email: str, plan_id: str | None = None, trace_id: str | None = None):
        self._session = session
        self.email = email
        self.plan_id = plan_id
        self.trace_id = trace_id  # the session UUID shown in the ArmorIQ console
        # one SDK call at a time per session, in order: a check, then its report and
        # flush. Without this a flush can close the next call's trace and the console
        # shows spans under the wrong turn.
        self._lock = asyncio.Lock()

    async def check(self, tool: str, args: dict, user_email: str | None = None) -> Check:
        name = tool if tool.startswith("counter__") else f"counter__{tool}"
        async with self._lock:
            r = await asyncio.to_thread(self._session.check, name, dict(args or {}),
                                        user_email=user_email or self.email)
        return Check(allowed=bool(r.allowed), action=str(r.action), reason=str(r.reason or ""),
                     delegation_id=getattr(r, "delegation_id", None),
                     matched_policy=getattr(r, "matched_policy", None))

    async def report(self, tool: str, args: dict, result: object, ok: bool = True,
                     error: str | None = None, ms: float | None = None, delegated: bool = False) -> None:
        """Record a tool run on the console's call ledger, then ship it. Never raises."""
        from armoriq_sdk.session import ReportOptions

        name = tool if tool.startswith("counter__") else f"counter__{tool}"
        opts = ReportOptions(status="success" if ok else "error", error_message=error, duration_ms=ms,
                             is_delegated=delegated or None)

        def run():
            self._session.report(name, dict(args or {}), result, opts)
            self._session.flush_observability()

        try:
            async with self._lock:
                await asyncio.to_thread(run)
        except Exception as e:
            log.warning("ArmorIQ report failed for %s: %s", tool, type(e).__name__)

    async def flush(self) -> None:
        """Ship the checks recorded so far (holds and blocks have no tool run)."""
        try:
            async with self._lock:
                await asyncio.to_thread(self._session.flush_observability)
        except Exception as e:
            log.warning("ArmorIQ flush failed: %s", type(e).__name__)

    async def close(self) -> None:
        try:
            async with self._lock:
                await asyncio.to_thread(self._session.close)
        except Exception as e:
            log.warning("ArmorIQ close failed: %s", type(e).__name__)


class ArmorIQLayer:
    """Creates ArmorIQ sessions and calls the delegation endpoints."""

    def __init__(self, api_key: str, login: str, backend: str = "https://api.armoriq.ai",
                 iap: str | None = None, agent_id: str = "counter-front-desk", telemetry: bool = True):
        if not api_key:
            raise RuntimeError("ARMORIQ_API_KEY is not set (add it to ~/.config/counter/.env)")
        if not login:
            raise RuntimeError("ARMORIQ_LOGIN is not set (add it to ~/.config/counter/.env)")
        from armoriq_sdk import ArmorIQClient

        self.backend = backend.rstrip("/")
        self._client = ArmorIQClient(api_key=api_key, backend_endpoint=self.backend,
                                     iap_endpoint=iap or None, agent_id=agent_id)
        self._api_key = api_key
        self.login = login
        self.agent_id = agent_id
        self.telemetry = telemetry

    async def start_session(self, email: str) -> ArmorIQSession:
        return await asyncio.to_thread(self._start, email)

    def _start(self, email: str) -> ArmorIQSession:
        from armoriq_sdk.session import ObservabilityConfig, SessionOptions

        from .tools import PLAN_ARGS, TOOL_NAMES

        trace_id = str(uuid.uuid4())
        opts = SessionOptions(mode="sdk", default_mcp_name="counter", validity_seconds=3600,
                              session_id=trace_id, llm="gemini-3.1-flash-live-preview")
        opts.observability = ObservabilityConfig(enabled=self.telemetry, endpoint=self.backend,
                                                 product="counter", agent_id=self.agent_id)
        plan = [{"name": f"counter__{t}", "args": PLAN_ARGS[t]} for t in TOOL_NAMES]
        last: Exception | None = None
        for attempt in range(2):
            try:
                session = self._client.for_user(email).start_session(opts)
                tok = session.start_plan(plan, goal="counter front desk")
                return ArmorIQSession(session, email, plan_id=getattr(tok, "plan_id", None), trace_id=trace_id)
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
