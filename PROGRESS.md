# Counter: progress

## State before the build (2026-10-17)
- ArmorIQ policy `counter-policy` switched to variant B and verified live (`reference/armoriq/out_verify_b.txt`): the three blocked tools block at $170 and $420 with `tool_not_in_allowlist`; refund $45 allow; exchange $170 hold; owner match $420 allow.
- Secrets in `~/.config/counter/.env`: ARMORIQ_API_KEY (probe-org), ARMORIQ_LOGIN, GEMINI_API_KEY, OWNER_PASSWORD, MCP_TOKEN. Slack tokens to come (new app from `slack/manifest.yaml`).
- Beat 3 (serial read off a real phone) is still unconfirmed; build both shop rules anyway.

## Done conditions
- **core** (`mcp_server/`, `backend/`): `uv run pytest` green in both; `backend/scripts/beats.py` drives `/api/tool` against the real MCP server and real ArmorIQ and prints, for the six beats: B1 match_competitor_quote 420 → ARMORIQ block; B2 issue_refund 45 (order 1101) → allow + refunded; B3 record_inspection with serial …7QK2 on order 1042 then issue_refund 799 → SHOP_RULE refused; B4 refund_to_account 45 on 1101 → ARMORIQ block, and a second issue_refund 45 on 1101 → SHOP_RULE already refunded; B5 process_exchange 170 → hold, then approve via `/api/pending/{id}/approve` → executed; B6 counter session match 420 → block, owner session match 420 → allow. No pending delegations left.
- **web** (`web/`): `?mock=1` run in a headless browser against the real backend shows every decision chip and the case card in the right state (screenshots saved to `web/shots/`); a live Gemini session connects with the ephemeral token, and a typed turn produces audio + a transcript + at least one tool call that reaches `/api/tool`.
- **slack**: unit tests with a fake Slack client pass; with real tokens, a hold posts a card and the Approve button executes the stored action once.

## Contract changes
(none yet)

## core

## web

## slack
Files: `backend/counter_backend/slack_bridge.py`, `backend/tests/test_slack_bridge.py`, `backend/scripts/slack_smoke.py`, `slack/manifest.yaml`, `slack/README.md`.

### How to set it up and run it
1. Create the Slack app from `slack/manifest.yaml` (api.slack.com → Create New App → From an app manifest), install it, generate an app-level token with `connections:write`, create `#counter-approvals` and `/invite @Counter`. Full steps in `slack/README.md`.
2. Put `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CHANNEL` in `~/.config/counter/.env`.
3. Unit tests (fake Slack client, no network):
   `uv run --with slack_bolt --with slack_sdk --with aiohttp --with pytest --with pytest-asyncio pytest backend/tests/test_slack_bridge.py`
4. Live smoke test (posts a fake hold card, waits for a click, prints the result):
   `uv run --with slack_bolt --with slack_sdk --with aiohttp --with python-dotenv python backend/scripts/slack_smoke.py`

### What was run (2026-10-17)
`uv run --with slack_bolt --with slack_sdk --with aiohttp --with pytest --with pytest-asyncio pytest backend/tests/test_slack_bridge.py -v`:
```
collected 12 items

backend/tests/test_slack_bridge.py::test_start_is_a_noop_without_tokens PASSED
backend/tests/test_slack_bridge.py::test_start_is_a_noop_without_channel PASSED
backend/tests/test_slack_bridge.py::test_registers_both_button_handlers PASSED
backend/tests/test_slack_bridge.py::test_post_hold_card_shape_and_still PASSED
backend/tests/test_slack_bridge.py::test_post_hold_without_still_skips_upload PASSED
backend/tests/test_slack_bridge.py::test_approve_click_calls_callback_once_and_swaps_buttons PASSED
backend/tests/test_slack_bridge.py::test_second_click_after_outcome_does_nothing PASSED
backend/tests/test_slack_bridge.py::test_double_click_while_first_is_in_flight_runs_the_action_once PASSED
backend/tests/test_slack_bridge.py::test_click_when_backend_says_already_handled PASSED
backend/tests/test_slack_bridge.py::test_users_info_failure_falls_back_to_owner PASSED
backend/tests/test_slack_bridge.py::test_reject_click_calls_callback_and_updates_card PASSED
backend/tests/test_slack_bridge.py::test_post_block_line PASSED

12 passed in 0.29s
```

The Slack app does not exist yet, so the live card click is still open. The smoke script was run in both states that exist now:
- No tokens in `~/.config/counter/.env` (checked without printing values): prints "Slack smoke test not run: SLACK_BOT_TOKEN, SLACK_APP_TOKEN and SLACK_CHANNEL are not in /Users/tayyabkhan/.config/counter/.env yet." and exits 1.
- With a throwaway fake app token: connects fail with `invalid_auth` (slack_sdk's own retry log), then prints "Could not connect over Socket Mode, so button clicks would not arrive. Check SLACK_APP_TOKEN ..." and exits 1.
- With real tokens the card posts and the wait-for-click path runs; that is the next check once the app is created.

Notes: `SlackBridge` matches CONTRACTS 4 (start / post_hold / post_block / update_hold). It adds one keyword-only `client` parameter so tests can inject a fake Slack client; no contract change. The approve click calls `on_approve` once, `users.info` gives the real name (fallback "Owner"), and the card ends as "Approved once by <name> · <result status>" / "Rejected by <name>" / "Already handled"; double clicks (including two clicks racing) never run the action twice. Tokens are never logged.
