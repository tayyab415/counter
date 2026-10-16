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

Core-side additions (no breaking changes; made so Slack/console get the same data):
- `POST /api/tool` accepts an optional `customer_said` string in the body. Reason: the Slack hold card and the owner console show the customer's last words; the web app may omit it.
- Owner `/api/session` response includes `owner_token` (the value for `X-Owner-Token`); `GET /api/events` accepts the owner token as `?token=` or `X-Owner-Token` header. Reason: `EventSource` cannot set headers, so the query param must work.
- `Decision.ts` is an ISO-8601 UTC string, `ms` a number. `GET /api/pending` returns a bare JSON list of PendingAction objects (no wrapper), stills stay out of it (`has_still` flag only).
- MCP server: custom routes `POST /reset` and `GET /healthz` sit next to `/mcp`; `/reset` does its own bearer check because FastMCP's auth wrapper only covers the MCP endpoint.

## core

Built on `build/core`: `mcp_server/` (FastMCP, the 11 tools, SQLite seed, bearer auth, `POST /reset`, `GET /healthz`) and `backend/` (FastAPI gate: shop rules → live ArmorIQ → real MCP, pending approvals, SSE, case cards, Gemini token), plus `backend/scripts/dev.sh`, `beats.py`, `live_check.py`.

### How to run

```
cd backend && ./scripts/dev.sh                    # MCP on :8001, backend on :8000 (dev.sh needs any bash; no `wait -n` on macOS bash 3.2)
cd mcp_server && uv run pytest -q                 # 9 tests
cd backend && uv run pytest -q                    # 55 tests (fakes, no network)
cd backend && uv run python scripts/beats.py      # six beats: live ArmorIQ + real MCP over HTTP; exit 0 when all pass
cd backend && uv run python scripts/live_check.py # backend-minted Gemini token opens a Live session; audio verified
```

### Tests (real output)

```
mcp_server$ uv run pytest -q
.........                                                                [100%]
9 passed in 1.19s

backend$ uv run pytest -q
.......................................................                  [100%]
55 passed in 0.43s
```

### Beats (real output, `uv run python scripts/beats.py` with `dev.sh` up)

```
backend http://127.0.0.1:8000 · ArmorIQ https://api.armoriq.ai
reset: ok
sessions: counter counter@shop (ses-63277ee2) · owner owner · Sam (ses-9efb456c)

B1 unlocked phone + printed $420 quote: match_competitor_quote 420 -> ARMORIQ block
    match_competitor_quote $420 -> ARMORIQ / block / tool_not_in_allowlist / 801ms
  B1 PASS  expect ARMORIQ block | got ARMORIQ/block tool_not_in_allowlist (blocked_by_policy)

B2 $45 charger return, order 1101: issue_refund 45 -> allow + refunded
    issue_refund $45 -> ARMORIQ / allow / allow_with_logging / 1134ms
  B2 PASS  expect ARMORIQ allow + refunded | got ARMORIQ/allow refunded -> Visa ending 6604

B3 wrong phone for order 1042 (serial ...7QK2 on camera): refund must be refused by shop rules
    record_inspection -> ARMORIQ / allow / allow_with_logging / 780ms
    issue_refund $799 -> SHOP_RULE / refused / serial on camera doesn't match the order / 0ms
  B3 PASS  expect inspection recorded, then SHOP_RULE refused (serial mismatch) | got inspection=recorded, refund SHOP_RULE/refused serial on camera doesn't match the order

B4 send it to my husband's card: refund_to_account 45 -> ARMORIQ block; repeat refund 45 -> already refunded
    refund_to_account $45 -> ARMORIQ / block / tool_not_in_allowlist / 765ms
    issue_refund $45 -> SHOP_RULE / refused / this order was already refunded / 0ms
  B4 PASS  expect refund_to_account ARMORIQ block + second refund SHOP_RULE refused | got refund_to_account ARMORIQ/block tool_not_in_allowlist; refund #2 SHOP_RULE/refused this order was already refunded

B5 upgrade, $170 gap: process_exchange 170 -> HOLD, owner approves in Slack/console -> executed once
    record_inspection -> ARMORIQ / allow / allow_with_logging / 797ms
    process_exchange $170 -> ARMORIQ / hold / amount_requires_approval / 1710ms
    approve pen-ef60181ffcb46b15 -> 200 / executed / armoriq 201 / 1604ms
  B5 PASS  expect HOLD then approve -> executed once (charged 170) | got hold hold deleg c30d1ca9, approve -> executed

B6 same tool, same amount: counter match 420 -> block; owner match 420 -> allow
    match_competitor_quote $420 -> ARMORIQ / block / tool_not_in_allowlist / 813ms
    match_competitor_quote $420 -> ARMORIQ / allow / enforcement_passed / 849ms
  B6 PASS  expect counter block, owner allow | got counter block tool_not_in_allowlist; owner allow enforcement_passed -> matched

cleanup: reject delegations left pending
  (none left pending)

=== summary ===
B1       PASS  ARMORIQ block | got: ARMORIQ/block tool_not_in_allowlist (blocked_by_policy)
B2       PASS  ARMORIQ allow + refunded | got: ARMORIQ/allow refunded -> Visa ending 6604
B3       PASS  inspection recorded, then SHOP_RULE refused (serial mismatch) | got: inspection=recorded, refund SHOP_RULE/refused serial on camera doesn't match the order
B4       PASS  refund_to_account ARMORIQ block + second refund SHOP_RULE refused | got: refund_to_account ARMORIQ/block tool_not_in_allowlist; refund #2 SHOP_RULE/refused this order was already refunded
B5       PASS  HOLD then approve -> executed once (charged 170) | got: hold hold deleg c30d1ca9, approve -> executed
B6       PASS  counter block, owner allow | got: counter block tool_not_in_allowlist; owner allow enforcement_passed -> matched
ALL BEATS PASS
```

Cleanup detail: B5's delegation was approved at ArmorIQ, so it is resolved, not pending — rejecting an approved delegation answers 400 "already rejected"/"already resolved", so the driver only rejects genuinely pending ones. Reject path proven on a live hold left pending on purpose:

```
hold: hold amount_requires_approval deleg 6b699467 pending item: pen-8ff71b711f0e42ef
reject pending delegation: 201 {"delegationId":"6b699467-...","status":"rejected",...}
reject again: 400 {"message":"Delegation is already rejected",...}
```

### Gemini Live token (real output, `uv run python scripts/live_check.py`)

The script takes the token the backend mints in `/api/session`, opens `gemini-3.1-flash-live-preview` with it once per api version, sends one text turn, counts audio bytes back:

```
v1alpha  connect ok     audio 250082B  text 'I can answer questions, provide information, or chat about t'
v1beta   connect ok     audio 289922B  text 'I can help with questions, writing, summarizing, and more. I'

=== result ===
v1alpha  connect=True audio_bytes=250082 text="I can answer questions, provide information, or chat about things. What's on you"
v1beta   connect=True audio_bytes=289922 text='I can help with questions, writing, summarizing, and more. Is there anything spe'
WORKING apiVersion with an ephemeral token: v1alpha, v1beta
audio came back: yes
```

Mint side, same check (`auth_tokens.create` per api version, real output):

```
mint v1alpha: ok (auth_tokens/6a28...)
mint v1beta: ok (auth_tokens/55e6...)
```

Answer: both api versions work with the installed `google-genai`, for minting and for connecting with the token, and audio comes back on both. The SDK itself prints `The SDK's ephemeral token support is in v1alpha only` and recommends `HttpOptions(api_version="v1alpha")`, so v1alpha stays the default everywhere (backend `GEMINI_API_VERSION`, and `web/` should connect with `apiVersion: "v1alpha"`).

### Notes

- Shop-rule order, mapping and fail-closed behavior are exactly per `CONTRACTS.md`; the backend prefetches the order row over MCP for the serial rule (the seed lives only in the MCP server).
- Decision `ms` is total elapsed for the call path; SSE `decision` events carry the same object.
- 55 backend tests use fakes (no network); the beats run is the live end-to-end proof.

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
