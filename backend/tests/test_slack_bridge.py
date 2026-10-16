"""Unit tests for the Slack approval bridge.

Everything runs against a fake Slack client, so the tests never touch the
network. The button handlers are exercised through the same registration path
the app uses at runtime (a tiny fake App collects them).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from counter_backend.slack_bridge import (  # noqa: E402
    APPROVE_ACTION,
    REJECT_ACTION,
    SlackBridge,
)

PENDING = {
    "pending_id": "pend-1042-170",
    "session_id": "sess-1",
    "tool": "process_exchange",
    "args": {"order_id": "1042", "new_item": "iPhone 15 Pro 256GB", "amount": 170},
    "delegation_id": "deleg-1",
    "status": "waiting",
    "created_ts": 1760000000,
    "armoriq_reason": "amount over the counter limit of $100",
    "customer_said": "Can I just pay the difference?",
}


class FakeSlackClient:
    """Records chat/files/users calls. No network."""

    def __init__(self, names=None, fail_users_info=False):
        self.names = names if names is not None else {"U1": {"real_name": "Sam Owner"}}
        self.fail_users_info = fail_users_info
        self.posted: list[dict] = []
        self.updates: list[dict] = []
        self.uploads: list[dict] = []
        self._counter = 0

    async def chat_postMessage(self, *, channel, text=None, blocks=None, thread_ts=None):
        self._counter += 1
        ts = f"1760000000.{self._counter:06d}"
        self.posted.append(
            {"channel": channel, "text": text, "blocks": blocks, "thread_ts": thread_ts, "ts": ts}
        )
        return {"ok": True, "ts": ts, "channel": channel}

    async def chat_update(self, *, channel, ts, text=None, blocks=None):
        self.updates.append({"channel": channel, "ts": ts, "text": text, "blocks": blocks})
        return {"ok": True, "ts": ts}

    async def users_info(self, *, user):
        if self.fail_users_info:
            raise RuntimeError("users.info unavailable")
        return {"ok": True, "user": self.names.get(user, {})}

    async def files_upload_v2(self, *, channel, thread_ts=None, content=None, filename=None, alt_txt=None):
        self.uploads.append(
            {"channel": channel, "thread_ts": thread_ts, "content": content, "filename": filename}
        )
        return {"ok": True}


class FakeApp:
    """Collects app.action(...) listeners like slack_bolt's AsyncApp."""

    def __init__(self):
        self.actions: dict[str, callable] = {}

    def action(self, action_id):
        def decorate(fn):
            self.actions[action_id] = fn
            return fn

        return decorate


def make_bridge(on_approve, on_reject, *, client=None, channel="C-test"):
    client = client or FakeSlackClient()
    bridge = SlackBridge(
        "xoxb-test", "xapp-test", channel, on_approve, on_reject, client=client
    )
    app = FakeApp()
    bridge._register_handlers(app)
    return bridge, client, app


async def click(app, client, action_id, user_id="U1", pending_id=PENDING["pending_id"]):
    """Simulate one button click the way slack_bolt would call the handler."""
    acked = []

    async def ack():
        acked.append(True)

    handler = app.actions[action_id]
    await handler(
        ack=ack,
        body={"user": {"id": user_id}},
        action={"type": "button", "action_id": action_id, "value": pending_id},
        client=client,
    )
    return acked


def all_text(blocks):
    parts = []
    for block in blocks or []:
        text = block.get("text")
        if isinstance(text, dict) and "text" in text:
            parts.append(text["text"])
        for field in block.get("fields") or []:
            parts.append(field["text"])
    return "\n".join(parts)


# -- start ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_is_a_noop_without_tokens(caplog):
    async def callback(pending_id, approver_name):  # pragma: no cover
        raise AssertionError("no callback should run")

    bridge = SlackBridge("", "", "", callback, callback)
    with caplog.at_level(logging.INFO, logger="counter.slack"):
        await bridge.start()

    assert bridge._app is None
    assert bridge._client is None
    assert "slack: disabled" in caplog.text

    # Posting without Slack must not raise either.
    await bridge.post_hold(dict(PENDING), None, None)
    await bridge.post_block({"tool": "issue_refund", "args": {"amount": 45}, "reason": "x"})


@pytest.mark.asyncio
async def test_start_is_a_noop_without_channel():
    async def callback(pending_id, approver_name):  # pragma: no cover
        raise AssertionError("no callback should run")

    bridge = SlackBridge("xoxb-test", "xapp-test", "", callback, callback)
    await bridge.start()
    assert bridge._app is None


# -- card shape -------------------------------------------------------------


def test_registers_both_button_handlers():
    async def callback(pending_id, approver_name):  # pragma: no cover
        raise AssertionError("no callback should run")

    bridge = SlackBridge("xoxb-test", "xapp-test", "C-test", callback, callback)
    app = FakeApp()
    bridge._register_handlers(app)
    assert set(app.actions) == {APPROVE_ACTION, REJECT_ACTION}


@pytest.mark.asyncio
async def test_post_hold_card_shape_and_still():
    async def callback(pending_id, approver_name):  # pragma: no cover
        raise AssertionError("no callback should run")

    bridge, client, _ = make_bridge(callback, callback)
    await bridge.post_hold(dict(PENDING), b"\xff\xd8fake-jpeg", PENDING["customer_said"])

    assert len(client.posted) == 1
    post = client.posted[0]
    assert post["channel"] == "C-test"

    blocks = post["blocks"]
    assert blocks[0]["type"] == "header"
    assert blocks[0]["text"]["text"] == "Counter needs approval"

    fields_block = next(b for b in blocks if b["type"] == "section" and "fields" in b)
    labels = [f["text"].split("\n", 1)[0] for f in fields_block["fields"]]
    assert labels == [
        "*Tool*",
        "*Amount*",
        "*Order*",
        "*ArmorIQ reason*",
        "*Customer said*",
    ]
    field_text = all_text([fields_block])
    assert "`process_exchange`" in field_text
    assert "$170" in field_text
    assert "#1042" in field_text
    assert "amount over the counter limit of $100" in field_text
    assert "Can I just pay the difference?" in field_text

    actions = next(b for b in blocks if b["type"] == "actions")
    approve, reject = actions["elements"]
    assert approve["action_id"] == "counter_approve"
    assert approve["style"] == "primary"
    assert approve["text"]["text"] == "Approve once"
    assert approve["value"] == PENDING["pending_id"]
    assert reject["action_id"] == "counter_reject"
    assert reject["style"] == "danger"
    assert reject["text"]["text"] == "Reject"
    assert reject["value"] == PENDING["pending_id"]

    # The still goes into the card's thread, so a click still lands on the card.
    assert len(client.uploads) == 1
    assert client.uploads[0]["thread_ts"] == post["ts"]
    assert client.uploads[0]["content"] == b"\xff\xd8fake-jpeg"

    # The message ts is remembered for later edits.
    assert bridge._holds[PENDING["pending_id"]]["ts"] == post["ts"]


@pytest.mark.asyncio
async def test_post_hold_without_still_skips_upload():
    async def callback(pending_id, approver_name):  # pragma: no cover
        raise AssertionError("no callback should run")

    bridge, client, _ = make_bridge(callback, callback)
    await bridge.post_hold(dict(PENDING), None, None)
    assert len(client.posted) == 1
    assert client.uploads == []


# -- approve ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_click_calls_callback_once_and_swaps_buttons():
    calls = []

    async def on_approve(pending_id, approver_name):
        calls.append((pending_id, approver_name))
        return {**PENDING, "status": "executed", "result": {"status": "exchanged"}}

    async def on_reject(pending_id, approver_name):  # pragma: no cover
        raise AssertionError("on_reject should not be called")

    bridge, client, app = make_bridge(on_approve, on_reject)
    await bridge.post_hold(dict(PENDING), None, PENDING["customer_said"])

    acked = await click(app, client, APPROVE_ACTION)

    assert acked == [True]
    assert calls == [(PENDING["pending_id"], "Sam Owner")]

    assert len(client.updates) == 1
    update = client.updates[0]
    assert update["channel"] == "C-test"
    assert update["ts"] == client.posted[0]["ts"]
    assert "Approved once by Sam Owner · exchanged" in all_text(update["blocks"])
    assert not any(b["type"] == "actions" for b in update["blocks"])


@pytest.mark.asyncio
async def test_second_click_after_outcome_does_nothing():
    calls = []

    async def on_approve(pending_id, approver_name):
        calls.append(pending_id)
        return {**PENDING, "status": "executed", "result": {"status": "exchanged"}}

    async def on_reject(pending_id, approver_name):
        calls.append(pending_id)
        return {**PENDING, "status": "rejected"}

    bridge, client, app = make_bridge(on_approve, on_reject)
    await bridge.post_hold(dict(PENDING), None, None)

    await click(app, client, APPROVE_ACTION)
    updates_after_first = len(client.updates)
    await click(app, client, REJECT_ACTION)  # a stale second click

    assert calls == [PENDING["pending_id"]]
    assert len(client.updates) == updates_after_first
    assert "Approved once by Sam Owner · exchanged" in all_text(client.updates[0]["blocks"])


@pytest.mark.asyncio
async def test_double_click_while_first_is_in_flight_runs_the_action_once():
    calls = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def on_approve(pending_id, approver_name):
        calls.append(pending_id)
        started.set()
        await release.wait()
        return {**PENDING, "status": "executed", "result": {"status": "exchanged"}}

    async def on_reject(pending_id, approver_name):  # pragma: no cover
        raise AssertionError("on_reject should not be called")

    bridge, client, app = make_bridge(on_approve, on_reject)
    await bridge.post_hold(dict(PENDING), None, None)

    first = asyncio.create_task(click(app, client, APPROVE_ACTION))
    await started.wait()
    second = asyncio.create_task(click(app, client, APPROVE_ACTION))
    await asyncio.sleep(0.01)  # let the second click finish its ack
    release.set()
    await asyncio.gather(first, second)

    assert calls == [PENDING["pending_id"]]
    # The racing second click saw "Already handled"; the first click's outcome wins.
    assert any("Already handled" in all_text(u["blocks"]) for u in client.updates)
    assert "Approved once by Sam Owner · exchanged" in all_text(client.updates[-1]["blocks"])


@pytest.mark.asyncio
async def test_click_when_backend_says_already_handled():
    async def on_approve(pending_id, approver_name):
        return None  # the backend already resolved this hold elsewhere

    async def on_reject(pending_id, approver_name):  # pragma: no cover
        raise AssertionError("on_reject should not be called")

    bridge, client, app = make_bridge(on_approve, on_reject)
    await bridge.post_hold(dict(PENDING), None, None)
    await click(app, client, APPROVE_ACTION)

    assert "Already handled" in all_text(client.updates[0]["blocks"])
    assert not any(b["type"] == "actions" for b in client.updates[0]["blocks"])


@pytest.mark.asyncio
async def test_users_info_failure_falls_back_to_owner():
    calls = []

    async def on_approve(pending_id, approver_name):
        calls.append((pending_id, approver_name))
        return {**PENDING, "status": "executed", "result": {"status": "exchanged"}}

    async def on_reject(pending_id, approver_name):  # pragma: no cover
        raise AssertionError("on_reject should not be called")

    client = FakeSlackClient(fail_users_info=True)
    bridge, client, app = make_bridge(on_approve, on_reject, client=client)
    await bridge.post_hold(dict(PENDING), None, None)
    await click(app, client, APPROVE_ACTION)

    assert calls == [(PENDING["pending_id"], "Owner")]
    assert "Approved once by Owner · exchanged" in all_text(client.updates[0]["blocks"])


# -- reject -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_click_calls_callback_and_updates_card():
    calls = []

    async def on_approve(pending_id, approver_name):  # pragma: no cover
        raise AssertionError("on_approve should not be called")

    async def on_reject(pending_id, approver_name):
        calls.append((pending_id, approver_name))
        return {**PENDING, "status": "rejected"}

    bridge, client, app = make_bridge(on_approve, on_reject)
    await bridge.post_hold(dict(PENDING), None, PENDING["customer_said"])

    acked = await click(app, client, REJECT_ACTION)

    assert acked == [True]
    assert calls == [(PENDING["pending_id"], "Sam Owner")]
    assert len(client.updates) == 1
    assert "Rejected by Sam Owner" in all_text(client.updates[0]["blocks"])
    assert not any(b["type"] == "actions" for b in client.updates[0]["blocks"])


# -- blocks -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_block_line():
    async def callback(pending_id, approver_name):  # pragma: no cover
        raise AssertionError("no callback should run")

    bridge, client, _ = make_bridge(callback, callback)
    await bridge.post_block(
        {
            "tool": "refund_to_account",
            "args": {"amount": 45},
            "armoriq_reason": "tool_not_in_allowlist",
        }
    )

    assert len(client.posted) == 1
    assert (
        client.posted[0]["text"]
        == ":no_entry: ArmorIQ blocked `refund_to_account` · $45 · tool_not_in_allowlist"
    )
    assert client.posted[0]["blocks"] is None
