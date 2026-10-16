# Slack bridge

Holds from ArmorIQ land in `#counter-approvals` as a Block Kit card with the
camera still, the tool, the amount and the customer's words. The owner presses
**Approve once** or **Reject**; the backend then re-checks the stored action
with ArmorIQ and executes it once.

The bridge is `backend/counter_backend/slack_bridge.py`. It uses slack_bolt in
Socket Mode, so there is no public URL and no request URL to set. The backend
process must stay running for buttons to work.

## Set up the Slack app (one time, about 5 minutes)

1. Go to <https://api.slack.com/apps> → **Create New App** → **From an app
   manifest** → pick your workspace → paste the contents of
   `slack/manifest.yaml` → **Create**.
2. **OAuth & Permissions** → **Install to Workspace** → **Allow**. Copy the
   **Bot User OAuth Token** (starts with `xoxb-`).
3. **Basic Information** → **App-Level Tokens** → **Generate Token and
   Scopes** → name it `counter-socket`, add the scope `connections:write` →
   **Generate**. Copy the token (starts with `xapp-`).
4. Create the channel `#counter-approvals`. This is the feed for hold cards and
   block notes. Invite the bot from inside the channel: `/invite @Counter`.
   Get the channel ID: right-click the channel → **Copy link**; the last piece
   of the URL is the ID (starts with `C`). Or: channel name → **View channel
   details** → **About** → bottom of the panel.
5. Put the three values in `~/.config/counter/.env` (create the file if it does
   not exist), one per line:

   ```
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_APP_TOKEN=xapp-...
   SLACK_CHANNEL=C...
   ```

   This file is outside the repo and gitignored. Never commit or paste these
   values anywhere else.

If the backend or a test runs with `COUNTER_ENV_FILE` set, it loads that file
instead.

## Check it works

With the three values in place, from the repo root:

```sh
uv run --with slack_bolt --with slack_sdk --with aiohttp --with python-dotenv \
    python backend/scripts/slack_smoke.py
```

The script posts a fake hold card ($170 exchange on order #1042) with a small
still, then waits up to 5 minutes for a click. Click **Approve once** or
**Reject**; the card turns into `Approved once by <you> · exchanged` or
`Rejected by <you>`, and the script prints the result and exits 0. It is a
fake pending: nothing is executed on ArmorIQ or the MCP server, and no
delegation is created. Without tokens it prints a "not run" message and exits
1; with a bad app token it prints a Socket Mode hint.

Unit tests, no network involved (fake Slack client):

```sh
uv run --with slack_bolt --with slack_sdk --with aiohttp --with pytest --with pytest-asyncio \
    pytest backend/tests/test_slack_bridge.py
```

## How the backend uses it

- In the lifespan: `SlackBridge(bot_token, app_token, channel, on_approve,
  on_reject)` and `await bridge.start()`. If any of the three values is
  missing, `start()` logs one line and returns; `post_hold` / `post_block`
  become no-ops, so the backend runs without Slack.
- `on_approve` / `on_reject` are async functions
  `(pending_id, approver_name) -> PendingAction | None`, the backend's own
  approve/reject path (same code as `POST /api/pending/{id}/approve`). They
  return `None` if the hold was already handled.
- `post_hold(pending, still_jpeg, customer_said)` posts the card and uploads
  the still into the card's thread.
- `post_block(decision)` posts the one-line block note, e.g.
  :no_entry: ArmorIQ blocked `refund_to_account` · $45 · tool_not_in_allowlist.
- `update_hold(pending)` edits the card when a pending resolved elsewhere
  (for example the owner console approves or rejects).
- On a click the handler shows the approver's real name from
  `users.info` (fallback: `Owner`), calls the callback once, and replaces the
  buttons with the outcome. A second click (or a race between two clicks)
  never runs the action again; the card says `Already handled`.

## Files

- `slack/manifest.yaml` — paste into **Create New App → From an app manifest**.
- `backend/counter_backend/slack_bridge.py` — the bridge.
- `backend/tests/test_slack_bridge.py` — offline tests with a fake Slack client.
- `backend/scripts/slack_smoke.py` — smoke test with real tokens.

Scopes come from the manifest: `chat:write`, `files:write`, `channels:read`,
`groups:read`, `users:read`, plus the app-level `connections:write`. No token
value is ever logged.
