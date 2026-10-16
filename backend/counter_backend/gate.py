"""The gate: shop rules, then ArmorIQ session.check(), then the MCP call.

Holds become PendingActions; approvals re-check the stored args and execute once.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time

from . import rules
from .store import CounterSession, Store, new_id, now_iso, pending_view
from .tools import TOOL_NAMES_SET

log = logging.getLogger("counter.gate")

HOLD_NOTE = "Tell the customer the owner has to approve this; it is not done yet."
BLOCK_NOTE = "This account can't do this. Offer what you can do instead."

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
        if name in KIND and case.get("kind") != KIND[name]:
            case["kind"] = KIND[name]
            changed = True
        if name in OUTCOME_TOOLS:
            outcome = self._outcome(name, args, decision, response)
            if outcome is not None:
                case["outcome"] = outcome
                changed = True
        return changed

    # ---------------------------------------------------------------- slack
    def _slack_task(self, method: str, *args) -> None:
        if self.slack is None:
            return
        task = asyncio.create_task(self._slack_call(method, *args))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
        t0 = time.perf_counter()
        args = dict(args or {})
        still = decode_still(still_b64)

        # 1. unknown tool
        if name not in TOOL_NAMES_SET:
            return await self._refuse(session, name, args, "unknown tool", t0, unknown_tool=True)

        # internal prep: learn the order's serial_last4 (server side only)
        if name in SERIAL_TOOLS or (name == "record_inspection" and rules.oid(args.get("order_id"))):
            await self._ensure_serial_last4(session, args)

        # 2. shop rules
        reason = rules.shop_rule_refusal(self.store, session, name, args, camera_on)
        if reason:
            return await self._refuse(session, name, args, reason, t0)

        # 3. ArmorIQ
        try:
            chk = await session.armor_session.check(name, args, user_email=session.identity)
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
                        "reason": chk.reason or "amount_requires_approval", "owner_paged": True,
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
                        "reason": chk.reason or "blocked", "note": BLOCK_NOTE}
            decision = self._decision(session, name, args, "ARMORIQ", "block", response["reason"],
                                      self._ms(t0), armoriq_reason=chk.reason)
            await self._finish(session, response, decision, name, args, None)
            return {"response": response, "decision": decision}

        # 4. the MCP tool runs
        try:
            full = await self.mcp.call_tool(name, args)
        except Exception as e:
            log.warning("MCP call failed for %s: %s", name, type(e).__name__)
            response = {"status": "error", "reason": "tool call failed"}
            decision = self._decision(session, name, args, "ARMORIQ", "block", "tool call failed",
                                      self._ms(t0), armoriq_reason=f"{type(e).__name__}: {e}")
            await self._finish(session, response, decision, name, args, None)
            return {"response": response, "decision": decision}
        self._note_result(session, name, args, full)
        response = self._shape_response(name, full)
        decision = self._decision(session, name, args, "ARMORIQ", "allow", chk.reason or "allowed",
                                  self._ms(t0), armoriq_reason=chk.reason, result=response)
        await self._finish(session, response, decision, name, args, full)
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
        try:
            chk = await session.armor_session.check(p["tool"], p["args"], user_email=session.identity)
        except Exception as e:
            return self._fail_pending(p, f"re-check failed: {type(e).__name__}")
        if chk.action != "allow" or not chk.allowed:
            return self._fail_pending(p, f"re-check did not allow: {chk.reason or chk.action}")
        try:
            result = await self.mcp.call_tool(p["tool"], p["args"])
        except Exception as e:
            return self._fail_pending(p, f"tool call failed: {type(e).__name__}")
        p["status"] = "executed"
        p["result"] = result
        p["resolved_ts"] = now_iso()
        self._note_result(session, p["tool"], p["args"], result)
        decision = self._decision(session, p["tool"], p["args"], "ARMORIQ", "allow",
                                  chk.reason or "approved by the owner", self._ms(t0),
                                  armoriq_reason=chk.reason, delegation_id=p["delegation_id"],
                                  pending_id=pending_id, result=result)
        await self._finish(session, result, decision, p["tool"], p["args"], result)
        self.store.publish(session.session_id, "pending_resolved",
                           {"pending_id": pending_id, "session_id": session.session_id,
                            "status": "executed", "tool": p["tool"], "result": result})
        self._slack_task("update_hold", pending_view(p))
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
        self._slack_task("update_hold", pending_view(p))
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
        self._slack_task("update_hold", pending_view(p))
        return p

    # ---------------------------------------------------------------- reset
    async def reset(self) -> bool:
        ok = False
        try:
            ok = await self.mcp.reset()
        except Exception as e:
            log.warning("MCP reset failed: %s", type(e).__name__)
        self.store.refunded_orders.clear()
        self.store.pending.clear()
        self.store.decisions.clear()
        self.store.serial_last4.clear()
        self.store.sessions.clear()
        self.store.owner_tokens.clear()
        self.store.publish(None, "reset", {"ok": ok})
        return ok
