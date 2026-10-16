"""Post a fake hold card to the real Counter Slack channel and wait for a click.

Checks the wiring end to end without the backend or ArmorIQ: it posts the same
Block Kit card the bridge posts for a real hold, waits for you to press
"Approve once" or "Reject", and prints what happened. The callbacks are fake,
so nothing is executed on ArmorIQ or the MCP server and no delegation is left
pending.

Run from the repo root:

    uv run --with slack_bolt --with slack_sdk --with aiohttp --with python-dotenv \
        python backend/scripts/slack_smoke.py

Needs SLACK_BOT_TOKEN, SLACK_APP_TOKEN and SLACK_CHANNEL in
~/.config/counter/.env (see slack/README.md). Without them it prints a short
message and exits without posting anything.
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from counter_backend.slack_bridge import SlackBridge  # noqa: E402

ENV_FILE = Path(os.environ.get("COUNTER_ENV_FILE", "~/.config/counter/.env")).expanduser()

# A 1x1 JPEG, so the still-upload path is exercised too.
TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIy"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIA"
    "AhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQA"
    "AAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3"
    "ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWm"
    "p6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oADAMB"
    "AAIRAxEAPwD3+iiigD//2Q=="
)

HOLD = {
    "pending_id": "smoke-" + secrets.token_hex(4),
    "session_id": "smoke-session",
    "tool": "process_exchange",
    "args": {"order_id": "1042", "new_item": "iPhone 15 Pro 256GB", "amount": 170},
    "delegation_id": "smoke-delegation",
    "status": "waiting",
    "armoriq_reason": "above the counter limit of $100; the owner has to approve",
    "customer_said": "Can I just pay the difference?",
}


async def main() -> int:
    load_dotenv(ENV_FILE)
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    app_token = os.environ.get("SLACK_APP_TOKEN")
    channel = os.environ.get("SLACK_CHANNEL")
    if not (bot_token and app_token and channel):
        print("Slack smoke test not run: SLACK_BOT_TOKEN, SLACK_APP_TOKEN and")
        print(f"SLACK_CHANNEL are not in {ENV_FILE} yet.")
        print("Create the app from slack/manifest.yaml and follow slack/README.md,")
        print("then run this script again.")
        return 1

    clicked = asyncio.Event()
    answer: dict = {}

    async def on_approve(pending_id, approver_name):
        print(f"\nApprove clicked by {approver_name}.")
        print(
            "With the real backend this would approve the delegation, re-check "
            "ArmorIQ on the stored action and execute it once."
        )
        answer.update(action="approve", by=approver_name)
        clicked.set()
        return {
            **HOLD,
            "status": "executed",
            "result": {"status": "exchanged"},
            "approver_name": approver_name,
        }

    async def on_reject(pending_id, approver_name):
        print(f"\nReject clicked by {approver_name}.")
        print("With the real backend this would reject the delegation and tell the customer.")
        answer.update(action="reject", by=approver_name)
        clicked.set()
        return {**HOLD, "status": "rejected", "approver_name": approver_name}

    bridge = SlackBridge(bot_token, app_token, channel, on_approve, on_reject)
    await bridge.start()
    try:
        connected = False
        for _ in range(40):  # up to ~10 s
            handler = bridge._handler
            if handler is not None and await handler.client.is_connected():
                connected = True
                break
            await asyncio.sleep(0.25)
        if not connected:
            print("Could not connect over Socket Mode, so button clicks would not arrive.")
            print("Check SLACK_APP_TOKEN (App-Level Token with connections:write) and")
            print("that Socket Mode is enabled in the app settings (slack/README.md).")
            return 1

        await bridge.post_hold(dict(HOLD), TINY_JPEG, HOLD["customer_said"])
        print(f"Posted a fake hold card to {channel}: process_exchange $170 on order #1042.")
        print("Click 'Approve once' or 'Reject' in Slack. Waiting up to 5 minutes...")
        try:
            await asyncio.wait_for(clicked.wait(), timeout=300)
        except asyncio.TimeoutError:
            print("No click after 5 minutes. Nothing was changed.")
            return 1
        await asyncio.sleep(1.0)  # let the card update land
        print(
            f"Card updated in Slack ({answer.get('action')} by {answer.get('by')}). "
            "Bridge wiring works."
        )
        return 0
    finally:
        if bridge._handler is not None:
            try:
                await bridge._handler.close_async()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
