"""ArmorIQ gate: one session per conversation, its plan (intake, then the open
case, see cases.py), checks, delegation approve/reject.

The SDK is sync; every call runs in a thread. `mode="sdk"`, policy `counter-policy`
(armor.io/v2, `deploy/armoriq/counter-policy.v2.json`) in the ArmorTools workspace.
Telemetry is on: each customer conversation is one session in the console at
tools.armoriq.ai, with every check and every tool run on it.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass

import httpx

log = logging.getLogger("counter.armor")


MONEY_RULES = {"issue_payout", "issue_refund", "process_exchange"}
NEVER_COUNTER = {"match_competitor_quote", "waive_lock_check", "refund_to_account"}
_RULE = re.compile(r"^(allow|hold|block):([a-z_.]+)#(\d+)$")


DRIFT_PREFIX = "tool-not-in-plan"
DRIFT_WORDS = "not in the plan for this case: intent drift (tool-not-in-plan)"


def is_drift(reason: str | None) -> bool:
    """The SDK's intent check: the tool was not declared in the captured plan."""
    return str(reason or "").startswith(DRIFT_PREFIX)


def describe(reason: str) -> str:
    """counter-policy rule ids in plain words, the raw id kept in brackets so it
    matches the ArmorIQ console: "hold:process_exchange#1" ->
    "over $100 needs the owner (hold:process_exchange#1)". Anything unknown comes
    back as the raw reason."""
    if is_drift(reason):
        return DRIFT_WORDS
    words = {"approved_delegation": "approved by the owner",
             "delegation_rejected": "the owner said no earlier in this conversation"}.get(reason)
    m = _RULE.match(reason or "")
    if m:
        verdict, tool, variant = m.group(1), m.group(2), int(m.group(3))
        if tool == "tools.default":
            words = "owner: every tool allowed" if verdict == "allow" else None
        elif tool in MONEY_RULES:
            words = {0: "over $350, the counter can't", 1: "over $100 needs the owner",
                     2: "up to $100, allowed"}.get(variant)
        elif tool in NEVER_COUNTER:
            words = "the counter may never use this"
        else:
            words = "allowed" if verdict == "allow" else None
    return f"{words} ({reason})" if words else reason


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

    async def start_plan(self, tools: list[str], goal: str, order_id: str | None = None) -> str | None:
        """Replace the plan on this same session (the console keeps one session per
        conversation). Calls to tools outside it come back `tool-not-in-plan`."""
        from .cases import plan_calls

        plan = plan_calls(tools, order_id)
        last: Exception | None = None
        async with self._lock:
            for attempt in range(2):
                try:
                    tok = await asyncio.to_thread(self._session.start_plan, plan, goal)
                    self.plan_id = getattr(tok, "plan_id", None)
                    return self.plan_id
                except Exception as e:
                    last = e
                    log.warning("ArmorIQ start_plan (%s) attempt %d failed: %s", goal, attempt + 1,
                                type(e).__name__)
                    await asyncio.sleep(0.75 * (attempt + 1))
        assert last is not None
        raise last

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

    async def record_generation(self, model: str, input_tokens: int, output_tokens: int,
                                cached_tokens: int = 0) -> None:
        """One Gemini turn's token counts, so the console shows usage per session."""
        def run():
            self._session.record_generation(model=model, input_tokens=int(input_tokens),
                                            output_tokens=int(output_tokens),
                                            cache_read_tokens=int(cached_tokens), cache_write_tokens=0,
                                            prompt=None, completion=None, finish_reason="stop")
            self._session.flush_observability()

        try:
            async with self._lock:
                await asyncio.to_thread(run)
        except Exception as e:
            log.warning("ArmorIQ record_generation failed: %s", type(e).__name__)

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

    async def start_session(self, email: str, tools: list[str] | None = None,
                            goal: str = "counter front desk") -> ArmorIQSession:
        """A session with its first plan: `tools` (default all 11)."""
        return await asyncio.to_thread(self._start, email, tools, goal)

    def _start(self, email: str, tools: list[str] | None, goal: str) -> ArmorIQSession:
        from armoriq_sdk.session import ObservabilityConfig, SessionOptions

        from .cases import plan_calls
        from .tools import TOOL_NAMES

        trace_id = str(uuid.uuid4())
        opts = SessionOptions(mode="sdk", default_mcp_name="counter", validity_seconds=3600,
                              session_id=trace_id, llm="gemini-3.1-flash-live-preview")
        opts.observability = ObservabilityConfig(enabled=self.telemetry, endpoint=self.backend,
                                                 product="counter", agent_id=self.agent_id)
        plan = plan_calls(list(tools or TOOL_NAMES))
        last: Exception | None = None
        for attempt in range(2):
            try:
                session = self._client.for_user(email).start_session(opts)
                tok = session.start_plan(plan, goal=goal)
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
