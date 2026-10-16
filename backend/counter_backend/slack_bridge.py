"""Slack approval bridge for Counter.

When ArmorIQ holds an action, the shop owner gets a Block Kit card in Slack
with "Approve once" / "Reject" buttons. The bridge is a slack_bolt AsyncApp in
Socket Mode, so the backend needs no public URL.

Ported from the CarePlus bot (reference/careplus/slack-bot.ts, TypeScript
Bolt). Setup: slack/manifest.yaml and slack/README.md. Tokens live in
~/.config/counter/.env and are never logged.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

logger = logging.getLogger("counter.slack")

HOLD_HEADER = "Counter needs approval"
APPROVE_ACTION = "counter_approve"
REJECT_ACTION = "counter_reject"

# (pending_id, approver_name) -> updated PendingAction, or None when the action
# was already handled by the time the click arrived.
ApprovalCallback = Callable[[str, str], Awaitable[dict | None]]


def _money(amount: Any) -> str | None:
    """170 -> "$170", 170.5 -> "$170.50", None when there is nothing to show."""
    if isinstance(amount, bool) or amount is None:
        return None
    if isinstance(amount, (int, float)):
        text = f"${amount:,.2f}"
        return text[:-3] if text.endswith(".00") else text
    text = str(amount).strip()
    return text or None


def _order_label(pending: dict) -> str | None:
    args = pending.get("args") or {}
    value = pending.get("order_id") or args.get("order_id") or args.get("order")
    if value in (None, ""):
        return None
    text = str(value)
    return text if text.startswith("#") else f"#{text}"


class SlackBridge:
    """Holds card + button handlers. Safe to construct and run without Slack."""

    def __init__(
        self,
        bot_token: str | None,
        app_token: str | None,
        channel: str | None,
        on_approve: ApprovalCallback,
        on_reject: ApprovalCallback,
        *,
        client: Any | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._app_token = app_token
        self._channel = channel
        self._on_approve = on_approve
        self._on_reject = on_reject
        self._client = client  # tests inject a fake; start() uses AsyncApp's
        self._app: AsyncApp | None = None
        self._handler: AsyncSocketModeHandler | None = None
        self._handler_task: asyncio.Task | None = None
        self._holds: dict[str, dict] = {}  # pending_id -> {channel, ts, blocks, state}
        self._approvers: dict[str, str] = {}  # pending_id -> who clicked
        self._resolved: set[str] = set()
        # Outcomes that arrived while the card was still being posted.
        self._early: dict[str, dict] = {}

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start the Socket Mode client in the background. No-op without tokens."""
        if not (self._bot_token and self._app_token and self._channel):
            logger.info(
                "slack: disabled (set SLACK_BOT_TOKEN, SLACK_APP_TOKEN and SLACK_CHANNEL to enable)"
            )
            return
        if self._handler_task is not None:
            return
        if self._app is None:
            self._app = AsyncApp(token=self._bot_token, logger=logger)
        if self._client is None:
            self._client = self._app.client
        self._register_handlers(self._app)
        self._handler = AsyncSocketModeHandler(self._app, self._app_token)
        self._handler_task = asyncio.create_task(self._run_socket_mode())
        logger.info("slack: bridge enabled (channel %s)", self._channel)

    async def _run_socket_mode(self) -> None:
        try:
            assert self._handler is not None
            await self._handler.start_async()  # connects, then sleeps forever
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "slack: socket mode stopped; approvals from Slack will not work"
            )

    def _register_handlers(self, app: Any) -> None:
        app.action(APPROVE_ACTION)(self._handle_approve)
        app.action(REJECT_ACTION)(self._handle_reject)

    # -- outbound ----------------------------------------------------------

    async def post_hold(
        self, pending: dict, still_jpeg: bytes | None, customer_said: str | None
    ) -> None:
        """Post the approval card and upload the camera still into its thread."""
        pending_id = str(pending.get("pending_id") or "")
        if self._client is None or not self._channel:
            logger.warning(
                "slack: bridge is not running; hold %s was not posted", pending_id
            )
            return
        if not pending_id:
            logger.warning("slack: hold without a pending_id was not posted")
            return
        tool = pending.get("tool") or "action"
        blocks = self._hold_blocks(pending, customer_said)
        try:
            response = await self._client.chat_postMessage(
                channel=self._channel,
                text=f"Counter needs approval: {tool}",
                blocks=blocks,
            )
        except Exception:
            logger.exception("slack: could not post the hold card for %s", pending_id)
            return
        ts = response.get("ts")
        channel = response.get("channel") or self._channel
        self._holds[pending_id] = {
            "channel": channel,
            "ts": ts,
            "blocks": blocks,
            "state": "waiting",
        }
        logger.info("slack: hold %s posted for %s", pending_id, tool)
        early = self._early.pop(pending_id, None)
        if early is not None:
            await self.update_hold(early)
        if still_jpeg:
            try:
                await self._client.files_upload_v2(
                    channel=channel,
                    thread_ts=ts,
                    content=still_jpeg,
                    filename="camera-still.jpg",
                    alt_txt="The customer's camera still from the counter",
                )
            except Exception:
                logger.warning(
                    "slack: still upload failed for hold %s", pending_id, exc_info=True
                )

    async def post_block(self, decision: dict) -> None:
        """One line into the channel: what ArmorIQ blocked and why."""
        if self._client is None or not self._channel:
            logger.warning("slack: bridge is not running; block was not posted")
            return
        args = decision.get("args") or {}
        tool = decision.get("tool") or "unknown"
        line = f":no_entry: ArmorIQ blocked `{tool}`"
        amount = _money(args.get("amount")) or _money(decision.get("amount"))
        if amount:
            line += f" · {amount}"
        reason = decision.get("armoriq_reason") or decision.get("reason")
        if reason:
            line += f" · {reason}"
        try:
            await self._client.chat_postMessage(channel=self._channel, text=line)
        except Exception:
            logger.exception("slack: could not post the block for %s", tool)

    async def update_hold(self, pending: dict) -> None:
        """Replace the buttons with the outcome once a hold is no longer waiting."""
        pending_id = str(pending.get("pending_id") or "")
        hold = self._holds.get(pending_id)
        if pending.get("status") == "waiting":
            return
        if hold is None:
            # The card may still be posting; post_hold applies this when it lands.
            if pending_id:
                self._early[pending_id] = dict(pending)
            logger.debug("slack: update_hold before the card for %s", pending_id)
            return
        self._resolved.add(pending_id)
        await self._edit_outcome(pending_id, self._outcome_text(pending))

    def _outcome_text(self, pending: dict) -> str:
        pending_id = str(pending.get("pending_id") or "")
        name = (
            pending.get("approver_name")
            or self._approvers.get(pending_id)
            or "Owner"
        )
        status = pending.get("status")
        if status == "rejected":
            return f"Rejected by {name}"
        if status == "executed":
            result = pending.get("result") or {}
            return f"Approved once by {name} · {result.get('status') or 'done'}"
        if status == "approved":
            return f"Approved once by {name}"
        if status == "failed":
            reason = (
                pending.get("reason") or pending.get("error") or "the action did not run"
            )
            return f"Approved once by {name} · failed: {reason}"
        return "Already handled"

    def _hold_blocks(self, pending: dict, customer_said: str | None) -> list[dict]:
        args = pending.get("args") or {}
        fields: list[tuple[str, str]] = [
            ("Tool", f"`{pending.get('tool') or 'unknown'}`")
        ]
        amount = _money(args.get("amount"))
        if amount:
            fields.append(("Amount", amount))
        order = _order_label(pending)
        if order:
            fields.append(("Order", order))
        reason = pending.get("armoriq_reason") or pending.get("reason")
        if reason:
            fields.append(("ArmorIQ reason", str(reason)))
        words = customer_said or pending.get("customer_said")
        if words:
            fields.append(("Customer said", f"“{words}”"))
        pending_id = str(pending.get("pending_id") or "")
        return [
            {"type": "header", "text": {"type": "plain_text", "text": HOLD_HEADER}},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*{label}*\n{value}"}
                    for label, value in fields
                ],
            },
            {
                "type": "actions",
                "block_id": f"counter_hold_{pending_id}",
                "elements": [
                    {
                        "type": "button",
                        "action_id": APPROVE_ACTION,
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Approve once"},
                        "value": pending_id,
                    },
                    {
                        "type": "button",
                        "action_id": REJECT_ACTION,
                        "style": "danger",
                        "text": {"type": "plain_text", "text": "Reject"},
                        "value": pending_id,
                    },
                ],
            },
        ]

    # -- button handling ---------------------------------------------------

    async def _handle_approve(self, ack, body, action, client) -> None:
        await self._handle_action(ack, body, action, client, approved=True)

    async def _handle_reject(self, ack, body, action, client) -> None:
        await self._handle_action(ack, body, action, client, approved=False)

    async def _handle_action(
        self, ack, body, action, client, *, approved: bool
    ) -> None:
        await ack()
        pending_id = str((action or {}).get("value") or "")
        hold = self._holds.get(pending_id)
        if not pending_id or hold is None:
            logger.warning("slack: click for unknown hold %r ignored", pending_id)
            return
        if pending_id in self._resolved or hold.get("state") != "waiting":
            # A second click (double tap, or a stale card) must never run the
            # action twice. If the card still shows buttons, stop showing them.
            if hold.get("state") == "waiting":
                await self._edit_outcome(pending_id, "Already handled")
            return
        user_id = ((body or {}).get("user") or {}).get("id")
        name = await self._approver_name(client, user_id)
        self._resolved.add(pending_id)
        self._approvers[pending_id] = name
        callback = self._on_approve if approved else self._on_reject
        try:
            pending = await callback(pending_id, name)
        except Exception:
            logger.exception(
                "slack: %s failed for hold %s",
                "approve" if approved else "reject",
                pending_id,
            )
            self._resolved.discard(pending_id)
            await self._note_in_thread(
                pending_id, "That click didn't go through. Try again."
            )
            return
        if pending is None:
            await self._edit_outcome(pending_id, "Already handled")
            return
        if pending.get("status") == "waiting":
            logger.warning("slack: hold %s came back still waiting", pending_id)
            self._resolved.discard(pending_id)
            await self._note_in_thread(
                pending_id, "That click didn't go through. Try again."
            )
            return
        pending = {**pending, "approver_name": pending.get("approver_name") or name}
        await self.update_hold(pending)

    async def _approver_name(self, client: Any, user_id: str | None) -> str:
        """Real name from users.info; "Owner" when the lookup fails."""
        if not user_id:
            return "Owner"
        try:
            response = await client.users_info(user=user_id)
            user = response.get("user") or {}
            return user.get("real_name") or user.get("name") or "Owner"
        except Exception:
            logger.warning("slack: users.info failed; using 'Owner'", exc_info=True)
            return "Owner"

    async def _edit_outcome(self, pending_id: str, outcome: str) -> None:
        hold = self._holds.get(pending_id)
        if hold is None or self._client is None or not hold.get("ts"):
            return
        hold["state"] = "resolved"
        blocks = [b for b in hold["blocks"] if b.get("type") != "actions"]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": outcome}})
        try:
            await self._client.chat_update(
                channel=hold["channel"],
                ts=hold["ts"],
                text=outcome,
                blocks=blocks,
            )
        except Exception:
            logger.exception("slack: could not update the hold card for %s", pending_id)
        else:
            logger.info("slack: hold card updated for %s: %s", pending_id, outcome)

    async def _note_in_thread(self, pending_id: str, text: str) -> None:
        hold = self._holds.get(pending_id)
        if hold is None or self._client is None or not hold.get("ts"):
            return
        try:
            await self._client.chat_postMessage(
                channel=hold["channel"], thread_ts=hold["ts"], text=text
            )
        except Exception:
            logger.exception("slack: could not post a note for hold %s", pending_id)
