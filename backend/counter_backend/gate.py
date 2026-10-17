"""The gate: shop rules, then ArmorIQ session.check(), then the MCP call.

Holds become PendingActions; approvals re-check the stored args and execute once.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time

from . import cases, rules
from .armor import describe, is_drift
from .store import CounterSession, Store, new_id, now_iso, pending_view
from .tools import OPEN_CASE, TOOL_NAMES_SET

log = logging.getLogger("counter.gate")

HOLD_NOTE = "Tell the customer the owner has to approve this; it is not done yet."
BLOCK_NOTE = "This account can't do this. Offer what you can do instead."
IDLE_S = 30 * 60  # a conversation with no tool call or usage for this long is over


def drift_note(session: CounterSession, name: str) -> str:
    if cases.in_case(session):
        return (f"{name} is outside the open case ({cases.describe_case(session.plan)}). "
                f"ArmorIQ only allows the actions planned for this case, so it did not run.")
    return (f"No case is open, and {name} is not an intake action, so ArmorIQ did not run it.")

SERIAL_TOOLS = rules.SERIAL_TOOLS
MONEY_TOOLS = rules.MONEY_TOOLS

# case-card kind per tool
KIND = {"issue_refund": "return", "refund_to_account": "return", "process_exchange": "exchange",
        "issue_payout": "trade_in", "match_competitor_quote": "trade_in", "lookup_price": "trade_in"}
OUTCOME_TOOLS = {"issue_refund", "refund_to_account", "process_exchange", "issue_payout",
                 "match_competitor_quote", "waive_lock_check", "record_inspection"}


def decode_still(still_b64: str | None) -> bytes | None:
    if not still_b64:
        return None
    s = still_b64.strip()
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    try:
        return base64.b64decode(s, validate=False)
    except Exception:
        return None


class Gate:
    def __init__(self, settings, store: Store, armor, mcp, slack=None):
        self.settings = settings
        self.store = store
        self.armor = armor
        self.mcp = mcp
        self.slack = slack
        self._tasks: set[asyncio.Task] = set()
        self.idle_s = IDLE_S

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _ms(t0: float) -> int:
        return int((time.perf_counter() - t0) * 1000)

    def _decision(self, session: CounterSession, tool: str, args: dict, layer: str, action: str,
                  reason: str, ms: int, *, armoriq_reason: str | None = None,
                  delegation_id: str | None = None, pending_id: str | None = None,
                  result: dict | None = None) -> dict:
        d = {"id": new_id("dec"), "ts": now_iso(), "session_id": session.session_id, "role": session.role,
             "tool": tool, "args": {k: v for k, v in (args or {}).items() if k != "still"},
             "layer": layer, "action": action, "reason": reason, "ms": int(ms)}
        for key, value in (("armoriq_reason", armoriq_reason), ("delegation_id", delegation_id),
                           ("pending_id", pending_id), ("result", result)):
            if value is not None:
                d[key] = value
        return d

    async def _finish(self, session: CounterSession, response: dict, decision: dict, name: str,
                      args: dict, full_result: dict | None) -> None:
        self.store.add_decision(decision)
        self.store.publish(session.session_id, "decision", decision)
        if self._update_case(session, name, args, response, decision, full_result):
            self.store.publish(session.session_id, "case",
                               {"session_id": session.session_id, "case": session.case})
        if decision["action"] == "block":
            self._slack_task("post_block", decision)

    async def _refuse(self, session, name, args, reason, t0, *, unknown_tool=False) -> dict:
        response = ({"status": "error", "reason": reason} if unknown_tool
                    else {"status": "refused_by_shop_rule", "decided_by": "shop rules", "reason": reason})
        decision = self._decision(session, name, args, "SHOP_RULE", "refused", reason, self._ms(t0))
        await self._finish(session, response, decision, name, args, None)
        return {"response": response, "decision": decision}

    async def _ensure_serial_last4(self, session, args: dict) -> None:
        order = rules.oid((args or {}).get("order_id"))
        if not order or order in self.store.serial_last4:
            return
        try:
            full = await self.mcp.call_tool("get_order", {"order_id": order})
        except Exception as e:
            log.warning("get_order prefetch failed: %s", type(e).__name__)
            return  # don't cache: try again next time
        if isinstance(full, dict) and "error" not in full:
            self.store.serial_last4[order] = full.get("serial_last4")
        else:
            self.store.serial_last4[order] = None

    def _note_result(self, session: CounterSession, name: str, args: dict, result: dict) -> None:
        if name == "get_order" and isinstance(result, dict) and result.get("order_id"):
            self.store.serial_last4[result["order_id"]] = result.get("serial_last4")
        if name == "record_inspection":
            order = (result or {}).get("order_id") or rules.oid((args or {}).get("order_id"))
            if order:
                session.inspections[order] = (result or {}).get("serial_seen") or (args or {}).get("serial_seen")
        if name in ("issue_refund", "refund_to_account") and (result or {}).get("status") == "refunded":
            order = rules.oid((args or {}).get("order_id"))
            if order:
                self.store.refunded_orders.add(order)

    def _shape_response(self, name: str, full: dict) -> dict:
        if name == "get_order" and isinstance(full, dict) and "order_id" in full:
            out = {k: v for k, v in full.items() if k not in ("serial", "serial_last4")}
            out["serial_check"] = "done by the system when you record the inspection"
            return out
        return full

    def _outcome(self, name: str, args: dict, decision: dict, response: dict):
        amount = rules.num((args or {}).get("amount"))
        action = decision["action"]
        if action == "refused":
            return {"status": "refused", "amount": amount, "detail": decision["reason"]}
        if action == "block":
            return {"status": "blocked", "amount": amount,
                    "detail": decision.get("armoriq_reason") or decision["reason"]}
        if action == "hold":
            return {"status": "waiting_for_owner", "amount": amount,
                    "detail": decision.get("armoriq_reason") or decision["reason"]}
        if name in ("issue_refund", "refund_to_account"):
            return {"status": "refunded", "amount": amount,
                    "detail": response.get("to") or response.get("refund_id")}
        if name == "process_exchange":
            return {"status": "exchanged", "amount": amount,
                    "detail": response.get("new_item") or response.get("exchange_id")}
        if name == "issue_payout":
            return {"status": "paid", "amount": amount, "detail": response.get("payout_id")}
        return None

    def _update_case(self, session: CounterSession, name: str, args: dict, response: dict,
                     decision: dict, full_result: dict | None) -> bool:
        case = session.case
        changed = False
        if name == "get_order" and decision["action"] == "allow" and isinstance(full_result, dict) \
                and full_result.get("order_id"):
            case["order"] = {"order_id": full_result["order_id"], "item": full_result["item"],
                             "price_paid": full_result["price_paid"], "payment": full_result["payment"],
                             "serial_last4": full_result["serial_last4"]}
            changed = True
        if name == "record_inspection" and decision["action"] == "allow":
            order = (full_result or {}).get("order_id") or rules.oid((args or {}).get("order_id"))
            seen = (args or {}).get("serial_seen") or (full_result or {}).get("serial_seen")
            last4 = self.store.serial_last4.get(order) if order else None
            if last4 is None and case.get("order") and case["order"].get("order_id") == order:
                last4 = case["order"].get("serial_last4")
            match = None
            if last4 and seen is not None:
                match = rules.alnum(seen).endswith(rules.alnum(last4))
            case["inspection"] = {"serial_seen": seen, "condition": (args or {}).get("condition"),
                                  "serial_match": match}
            changed = True
        if name in KIND and not cases.in_case(session) and case.get("kind") != KIND[name]:
            case["kind"] = KIND[name]
            changed = True
        if name in OUTCOME_TOOLS:
            outcome = self._outcome(name, args, decision, response)
            if outcome is not None:
                case["outcome"] = outcome
                changed = True
        return changed

    # ---------------------------------------------------------------- case plans
    def _publish_plan(self, session: CounterSession) -> None:
        self.store.publish(session.session_id, "plan", cases.plan_event(session))

    def _close_case(self, session: CounterSession | None, tool: str) -> None:
        """The case's money tool ran, or its hold was resolved: a new case may open.
        The closed case's plan stays in force until the next open_case, so a
        follow-up like notify_customer_on_file still runs."""
        if session is None or tool not in cases.CLOSING_TOOLS or not cases.case_open(session):
            return
        session.plan["status"] = "closed"
        session.plan["closed_ts"] = now_iso()
        self._publish_plan(session)

    def _waiting_hold(self, session: CounterSession) -> dict | None:
        for p in self.store.pending.values():
            if p["session_id"] == session.session_id and p["status"] == "waiting":
                return p
        return None

    async def start_intake(self, session: CounterSession) -> None:
        """Record the plan the ArmorIQ session was started with."""
        session.plan = cases.new_plan(cases.INTAKE, None, cases.intake_tools(session.role),
                                      getattr(session.armor_session, "plan_id", None))
        self._publish_plan(session)

    async def _open_case(self, session: CounterSession, args: dict, t0: float) -> dict:
        case_type = cases.normalize_type(args.get("case_type"))
        order = rules.oid(args.get("order_id"))
        if case_type not in cases.CASE_TYPES:
            return await self._refuse(session, OPEN_CASE, args, "case_type must be return, exchange or trade_in", t0)
        if case_type not in cases.NEEDS_ORDER:
            order = ""
        elif not order:
            return await self._refuse(session, OPEN_CASE, args,
                                      f"a {cases.LABEL[case_type]} needs the order number", t0)
        if self._waiting_hold(session):
            # the approve re-check runs on the current plan, so it must not change under a hold
            return await self._refuse(session, OPEN_CASE, args, "waiting for the owner on an earlier request", t0)
        if cases.case_open(session):
            cur = session.plan
            if cur["case_type"] == case_type and (cur.get("order_id") or "") == order:
                return {"response": self._case_response(session, "this case is already open"),
                        "decision": None, "plan": cases.plan_event(session)}
            return await self._refuse(session, OPEN_CASE, args,
                                      f"a case is already open: {cases.describe_case(cur)}", t0)

        tools = cases.case_tools(session.role, case_type)
        goal = cases.goal(case_type, order or None)
        plan_id = getattr(session.armor_session, "plan_id", None)
        if session.role != "owner":  # the owner keeps the plan with every tool
            try:
                plan_id = await session.armor_session.start_plan(tools, goal, order or None)
            except Exception as e:
                log.warning("ArmorIQ start_plan for %s failed: %s", goal, type(e).__name__)
                response = {"status": "error", "reason": "could not start the plan for this case; try again"}
                decision = self._decision(session, OPEN_CASE, args, "ARMORIQ", "block",
                                          "could not start the plan for this case", self._ms(t0),
                                          armoriq_reason=f"{type(e).__name__}: {e}")
                await self._finish(session, response, decision, OPEN_CASE, args, None)
                return {"response": response, "decision": decision}
        session.plan = cases.new_plan(case_type, order or None, tools, plan_id)
        keep = session.case.get("order") if order and (session.case.get("order") or {}).get("order_id") == order else None
        session.case = {"kind": case_type, "order": keep, "inspection": None, "outcome": None}
        self._publish_plan(session)
        self.store.publish(session.session_id, "case", {"session_id": session.session_id, "case": session.case})
        return {"response": self._case_response(session), "decision": None, "plan": cases.plan_event(session)}

    @staticmethod
    def _case_response(session: CounterSession, note: str | None = None) -> dict:
        p = session.plan
        return {"status": "case_open", "case_type": p["case_type"], "order_id": p.get("order_id"),
                "plan": list(p["tools"]),
                "note": note or "Case opened. Only the actions in this plan can run until it closes."}

    async def close_case(self, session: CounterSession) -> dict | None:
        """Operator "next customer": back to the intake plan. Not a model tool.
        Returns None if a hold is waiting (the plan must not change under it)."""
        if self._waiting_hold(session):
            return None
        tools = cases.intake_tools(session.role)
        goal = cases.goal(cases.INTAKE, None)
        plan_id = getattr(session.armor_session, "plan_id", None)
        if session.role != "owner" and cases.in_case(session):
            plan_id = await session.armor_session.start_plan(tools, goal, None)
        session.plan = cases.new_plan(cases.INTAKE, None, tools, plan_id)
        session.case = {"kind": None, "order": None, "inspection": None, "outcome": None}
        self._publish_plan(session)
        self.store.publish(session.session_id, "case", {"session_id": session.session_id, "case": session.case})
        return cases.plan_event(session)

    # ---------------------------------------------------------------- slack
    def _slack_task(self, method: str, *args) -> None:
        if self.slack is None:
            return
        task = asyncio.create_task(self._slack_call(method, *args))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---------------------------------------------------------------- ArmorIQ console
    def _armor_task(self, session: CounterSession, method: str, *args, **kwargs) -> None:
        """Ship telemetry for this call to the ArmorIQ console, off the request path."""
        fn = getattr(session.armor_session, method, None)
        if fn is None:
            return
        task = asyncio.create_task(fn(*args, **kwargs))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def record_usage(self, session: CounterSession, input_tokens: int, output_tokens: int,
                     cached_tokens: int = 0) -> None:
        session.last_seen = time.monotonic()
        self._armor_task(session, "record_generation", self.settings.gemini_model, input_tokens,
                         output_tokens, cached_tokens)

    # ---------------------------------------------------------------- call end
    def end_session(self, session: CounterSession) -> None:
        """The call is over. Close its ArmorIQ session (that stops the SDK's shipper
        thread) and forget it, unless a hold is still waiting for the owner."""
        session.ended = True
        self._retire(session)

    def expire_idle(self) -> None:
        """End conversations whose page closed without saying so."""
        cutoff = time.monotonic() - self.idle_s
        for s in list(self.store.sessions.values()):
            if s.last_seen < cutoff:
                self.end_session(s)

    def _retire(self, session: CounterSession | None) -> None:
        if session is None or not session.ended or session.active:
            return  # a call still running may yet create a hold that needs this session
        if any(p["session_id"] == session.session_id and p["status"] in ("waiting", "approved")
               for p in self.store.pending.values()):
            return  # the approval needs this session; retire it once the hold resolves
        if self.store.sessions.pop(session.session_id, None) is not None:
            self._armor_task(session, "close")

    async def _slack_call(self, method: str, *args) -> None:
        try:
            fn = getattr(self.slack, method)
            await asyncio.wait_for(fn(*args), timeout=15)
        except Exception as e:
            log.warning("slack %s failed: %s", method, type(e).__name__)

    # ---------------------------------------------------------------- tool gate
    async def handle_tool(self, session: CounterSession, name: str, args: dict, camera_on: bool,
                          still_b64: str | None = None, call_id: str | None = None,
                          customer_said: str | None = None) -> dict:
        session.active += 1
        try:
            return await self._handle_tool(session, name, args, camera_on, still_b64, customer_said)
        finally:
            session.active -= 1
            self._retire(session)

    async def _handle_tool(self, session: CounterSession, name: str, args: dict, camera_on: bool,
                           still_b64: str | None, customer_said: str | None) -> dict:
        t0 = time.perf_counter()
        session.last_seen = time.monotonic()
        args = dict(args or {})
        still = decode_still(still_b64)

        # 0. open_case: handled here, never sent to MCP or to ArmorIQ's check
        if name == OPEN_CASE:
            return await self._open_case(session, args, t0)

        # 1. unknown tool
        if name not in TOOL_NAMES_SET:
            return await self._refuse(session, name, args, "unknown tool", t0, unknown_tool=True)

        # internal prep: learn the order's serial_last4 (server side only)
        if name in SERIAL_TOOLS or (name == "record_inspection" and rules.oid(args.get("order_id"))):
            await self._ensure_serial_last4(session, args)

        # 2. shop rules
        reason = (rules.shop_rule_refusal(self.store, session, name, args, camera_on)
                  or cases.order_refusal(session, name, rules.oid(args.get("order_id"))))
        if reason:
            return await self._refuse(session, name, args, reason, t0)

        # ArmorIQ compares numbers: a "170" string would skip the hold rule, so send 170.
        if name in MONEY_TOOLS:
            amt = rules.num(args.get("amount"))
            args["amount"] = int(amt) if amt == int(amt) else amt

        # 3. ArmorIQ, then 4. the tool, as one turn on the console
        async with session.armor_session.turn() as turn:
            return await self._checked_call(session, turn, name, args, still, customer_said, t0)

    async def _checked_call(self, session, turn, name, args, still, customer_said, t0) -> dict:
        try:
            chk = await turn.check(name, args, user_email=session.identity)
        except Exception as e:
            log.warning("ArmorIQ check failed for %s: %s", name, type(e).__name__)
            response = {"status": "blocked_by_policy", "decided_by": "ArmorIQ",
                        "reason": "policy check unavailable", "note": BLOCK_NOTE}
            decision = self._decision(session, name, args, "ARMORIQ", "block", "policy check unavailable",
                                      self._ms(t0), armoriq_reason=f"{type(e).__name__}: {e}")
            await self._finish(session, response, decision, name, args, None)
            return {"response": response, "decision": decision}

        if chk.action == "hold":
            pending = {"pending_id": new_id("pen"), "session_id": session.session_id, "tool": name,
                       "args": args, "still": still, "delegation_id": chk.delegation_id,
                       "status": "waiting", "created_ts": now_iso(), "customer_said": customer_said}
            self.store.pending[pending["pending_id"]] = pending
            response = {"status": "held_for_approval", "decided_by": "ArmorIQ",
                        "reason": describe(chk.reason, name) or "over $100 needs the owner", "owner_paged": True,
                        "note": HOLD_NOTE}
            decision = self._decision(session, name, args, "ARMORIQ", "hold", response["reason"],
                                      self._ms(t0), armoriq_reason=chk.reason,
                                      delegation_id=chk.delegation_id, pending_id=pending["pending_id"])
            await self._finish(session, response, decision, name, args, None)
            self.store.publish(session.session_id, "pending", pending_view(pending))
            self._slack_task("post_hold", pending_view(pending), still, customer_said)
            return {"response": response, "decision": decision}

        if chk.action != "allow" or not chk.allowed:
            response = {"status": "blocked_by_policy", "decided_by": "ArmorIQ",
                        "reason": describe(chk.reason, name) or "blocked", "note": BLOCK_NOTE}
            if is_drift(chk.reason):
                response["note"] = drift_note(session, name)
                response["open_case"] = cases.describe_case(session.plan)
            decision = self._decision(session, name, args, "ARMORIQ", "block", response["reason"],
                                      self._ms(t0), armoriq_reason=chk.reason)
            await self._finish(session, response, decision, name, args, None)
            return {"response": response, "decision": decision}

        t_tool = time.perf_counter()
        try:
            full = await self.mcp.call_tool(name, args)
        except Exception as e:
            log.warning("MCP call failed for %s: %s", name, type(e).__name__)
            turn.report(name, args, None, ok=False, error=type(e).__name__, ms=self._ms(t_tool))
            response = {"status": "error", "reason": "tool call failed"}
            decision = self._decision(session, name, args, "ARMORIQ", "block", "tool call failed",
                                      self._ms(t0), armoriq_reason=f"{type(e).__name__}: {e}")
            await self._finish(session, response, decision, name, args, None)
            return {"response": response, "decision": decision}
        turn.report(name, args, full, ms=self._ms(t_tool))
        self._note_result(session, name, args, full)
        response = self._shape_response(name, full)
        decision = self._decision(session, name, args, "ARMORIQ", "allow", describe(chk.reason, name) or "allowed",
                                  self._ms(t0), armoriq_reason=chk.reason, result=response)
        await self._finish(session, response, decision, name, args, full)
        if isinstance(full, dict) and "error" not in full:
            self._close_case(session, name)
        return {"response": response, "decision": decision}

    # ---------------------------------------------------------------- approvals
    async def approve(self, pending_id: str, approver_name: str = "owner") -> dict | None:
        p = self.store.pending.get(pending_id)
        if not p or p["status"] != "waiting":
            return None
        session = self.store.sessions.get(p["session_id"])
        if session is None:
            return self._fail_pending(p, "session is gone")
        p["status"] = "approved"
        p["approver"] = approver_name
        t0 = time.perf_counter()
        try:
            status, body = await self.armor.approve_delegation(p["delegation_id"])
        except Exception as e:
            return self._fail_pending(p, f"ArmorIQ approve failed: {type(e).__name__}")
        if not (200 <= int(status) < 300):
            return self._fail_pending(p, f"ArmorIQ approve returned {status}")
        p["armoriq_approve_status"] = int(status)
        async with session.armor_session.turn() as turn:
            try:
                chk = await turn.check(p["tool"], p["args"], user_email=session.identity)
            except Exception as e:
                return self._fail_pending(p, f"re-check failed: {type(e).__name__}")
            if chk.action != "allow" or not chk.allowed:
                return self._fail_pending(p, f"re-check did not allow: {chk.reason or chk.action}")
            t_tool = time.perf_counter()
            try:
                result = await self.mcp.call_tool(p["tool"], p["args"])
            except Exception as e:
                turn.report(p["tool"], p["args"], None, ok=False, error=type(e).__name__,
                            ms=self._ms(t_tool), delegated=True)
                return self._fail_pending(p, f"tool call failed: {type(e).__name__}")
            turn.report(p["tool"], p["args"], result, ms=self._ms(t_tool), delegated=True)
        p["status"] = "executed"
        p["result"] = result
        p["resolved_ts"] = now_iso()
        self._note_result(session, p["tool"], p["args"], result)
        decision = self._decision(session, p["tool"], p["args"], "ARMORIQ", "allow",
                                  describe(chk.reason, p["tool"]) or "approved by the owner", self._ms(t0),
                                  armoriq_reason=chk.reason, delegation_id=p["delegation_id"],
                                  pending_id=pending_id, result=result)
        await self._finish(session, result, decision, p["tool"], p["args"], result)
        self.store.publish(session.session_id, "pending_resolved",
                           {"pending_id": pending_id, "session_id": session.session_id,
                            "status": "executed", "tool": p["tool"], "result": result})
        self._close_case(session, p["tool"])
        self._slack_task("update_hold", pending_view(p))
        self._retire(session)
        return p

    def _fail_pending(self, p: dict, reason: str) -> dict:
        p["status"] = "failed"
        p["detail"] = reason
        p["resolved_ts"] = now_iso()
        session = self.store.sessions.get(p["session_id"])
        self.store.publish(p["session_id"], "pending_resolved",
                           {"pending_id": p["pending_id"], "session_id": p["session_id"],
                            "status": "failed", "tool": p["tool"], "reason": reason})
        if session is not None:
            session.case["outcome"] = {"status": "refused", "amount": rules.num(p["args"].get("amount")),
                                       "detail": reason}
            self.store.publish(session.session_id, "case",
                               {"session_id": session.session_id, "case": session.case})
        self._close_case(session, p["tool"])
        self._slack_task("update_hold", pending_view(p))
        self._retire(session)
        return p

    async def reject(self, pending_id: str, approver_name: str = "owner",
                     reason: str = "declined by the owner") -> dict | None:
        p = self.store.pending.get(pending_id)
        if not p or p["status"] != "waiting":
            return None
        p["status"] = "rejected"
        p["approver"] = approver_name
        p["resolved_ts"] = now_iso()
        try:
            if p.get("delegation_id"):
                status, _ = await self.armor.reject_delegation(p["delegation_id"], reason)
                p["armoriq_reject_status"] = int(status)
        except Exception as e:
            p["armoriq_reject_status"] = f"error {type(e).__name__}"
        session = self.store.sessions.get(p["session_id"])
        if session is not None:
            session.case["outcome"] = {"status": "refused", "amount": rules.num(p["args"].get("amount")),
                                       "detail": f"{approver_name} declined {p['tool']}"}
            self.store.publish(session.session_id, "case",
                               {"session_id": session.session_id, "case": session.case})
        self.store.publish(p["session_id"], "pending_resolved",
                           {"pending_id": pending_id, "session_id": p["session_id"],
                            "status": "rejected", "tool": p["tool"]})
        self._close_case(session, p["tool"])
        self._slack_task("update_hold", pending_view(p))
        self._retire(session)
        return p

    # ---------------------------------------------------------------- reset
    async def reset(self) -> bool:
        ok = False
        try:
            ok = await self.mcp.reset()
        except Exception as e:
            log.warning("MCP reset failed: %s", type(e).__name__)
        for s in list(self.store.sessions.values()):
            self._armor_task(s, "close")
        self.store.refunded_orders.clear()
        self.store.pending.clear()
        self.store.decisions.clear()
        self.store.serial_last4.clear()
        self.store.sessions.clear()
        self.store.owner_tokens.clear()
        self.store.publish(None, "reset", {"ok": ok})
        return ok
