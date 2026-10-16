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
Web ↔ backend interface notes. Each one was checked against the dev stub and against the real
backend on :8000 (which is already compatible on all four).

1. Owner token over the query string, for the two places a browser cannot attach headers:
   `GET /api/events?session_id=*&token=<owner_token>` (EventSource cannot set headers) and
   `GET /api/pending/{id}/still.jpg?token=<owner_token>` (`<img>` cannot set headers). Everything
   else keeps `X-Owner-Token`. Verified on the real backend: both return 200 with `?token=`, 401
   without.
2. Owner auth: `POST /api/session` with `{"role": "owner", "password": ...}` returns the token as
   `owner_token` (verified on the real backend). The console stores it and sends `X-Owner-Token`.
   A reset clears sessions there, so the console is signed out by `/api/reset` and signs in again;
   the stub keeps owner tokens across a reset (credentials are not demo state).
3. `GET /api/pending` returns a bare list (CONTRACTS §2). The stub used to wrap it in
   `{"pending": [...]}`; both the stub and the console now follow the contract's list shape, and the
   console also accepts the wrapped shape.
4. Dev-stub only, ignored by the real backend: `POST /api/tool?auto_approve=1` (auto-approves a hold
   after ~2.5 s so the mock replay shows the waiting-card flip without an owner click),
   `GET /api/_dev_state` (counters + recent log for the browser checks; the live check falls back to
   page counters when it is absent), `?mock=1` replay.

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

Branch `build/web`. Everything is in `web/`; `reference/` untouched.

### How to run
    # fake backend + static web/ on port 8100 (the real backend owns 8000, the MCP server 8001)
    uv run --python 3.12 --with fastapi --with uvicorn --with google-genai --with python-dotenv \
        python web/dev_stub.py
    http://127.0.0.1:8100/            call screen (real Gemini Live session)
    http://127.0.0.1:8100/?mock=1     six-beat replay, scripted, no mic needed
    http://127.0.0.1:8100/owner.html  owner console (OWNER_PASSWORD)

    # browser checks, Chromium headless, screenshots into web/shots/
    uv run --python 3.12 --with playwright --with python-dotenv python web/check_web.py --live

    # same checks against the real backend on :8000 (see below: it needs its own web/ serving)
    uv run --python 3.12 --with playwright --with python-dotenv python web/check_web.py --real 8000

Every call is a relative `/api/...` URL, so the same page works on any port (point it at the real
backend by serving `web/` from it).

Files: `index.html` + `app.js` + `style.css` (call screen), `owner.html` (console), `dev_stub.py`
(backend fake), `check_web.py` (browser driver), `shots/`.

### Real vs canned
- Real: `/api/session` mints a real Gemini ephemeral token with `google-genai` (v1alpha, `uses:1`,
  30 min expiry, 2 min new-session window); the browser runs a real Gemini Live session
  (`gemini-3.1-flash-live-preview`, AUDIO, input/output transcription, the 11 tools from
  `reference/gemini/common.py`).
- Canned in the stub: policy decisions (rules copied from `reference/gemini/common.py` `Session.call`
  plus the CONTRACTS §2 gate order: shop rules → ArmorIQ), tool results, approval execution, case
  cards, SSE. Synthetic latencies (ARMORIQ allow ~0.55–0.95 s, hold ~1.3–1.9 s, shop-rule
  refusals ~20–90 ms) follow `reference/armoriq/FINDINGS.md`. The stub never talks to ArmorIQ or the
  MCP server.

### Tested, real output
Every run: 1920×1080, Chromium headless, `--use-fake-ui-for-media-stream
--use-fake-device-for-media-stream`. Output below is copied from the run logs (`shot …` lines
trimmed).

**a. Against the real backend** (real ArmorIQ policy, real MCP server). The backend instance that
was up on :8000 did not serve `web/` yet (`GET /` → 404), so `--real 8000` starts a small local proxy
on :8102 that serves the files from `web/` and streams `/api/*` to :8000 — page and API still share
one origin, SSE included.

    uv run --python 3.12 --with playwright --with python-dotenv python web/check_web.py --real 8000

```
proxy on http://127.0.0.1:8102 -> real backend http://127.0.0.1:8000, files from …/counter/build-web/web
   running against the real backend (no stub)

1. mock replay (?mock=1)
   owner console signed in
   demo reset before the replay: HTTP 200
   signed in again after the reset
   mock session ses-668cac0f5aaab752
   beat 1: ARMORIQ BLOCK on screen (match_competitor_quote -> block)
   beat 2: ARMORIQ ALLOW on screen (issue_refund -> allow)
   beat 2: screen shows 'Refunded'
   beat 3: SHOP RULE on screen (issue_refund -> refused)
   beat 3: screen shows '…7QK2'
   beat 3 case card: serial on order …4MD9 vs serial on camera …7QK2 ✗
   beat 4: ARMORIQ BLOCK on screen (refund_to_account -> block)
   beat 4: screen shows 'already refunded'
   beat 5: ARMORIQ HOLD on screen (process_exchange -> hold)
   beat 5: screen shows 'Waiting for owner'
   owner sees the pending card with the still
   approve once -> waiting card flipped on pending_resolved
   beat 6: ARMORIQ BLOCK on screen (match_competitor_quote -> block)
   rails: 13 decisions on screen

2. live Gemini session (no mock)
   Gemini Live session open (ephemeral token accepted)
   typed turn sent: Hi, I want to return a charger, order 1101
   audio arrived: yes 17 chunks, 3230 ms
   output transcript: yes 10 chunks
   tool call reached /api/tool: yes 1 sent by the page, decisions came back (no /api/_dev_state)
   decision on the rail: yes
   caption on screen: You Hi, I want to return a charger, order 1101
     Counter I can help with that. Could you hold the charger up to the camera so I can take a look?
     Thanks!

3. token apiVersion probe
   token + v1alpha: {"setupComplete": true, "firstMessages": ["setupComplete"], "events": ["socket open"]}
   token + v1beta:  {"setupComplete": true, "firstMessages": ["setupComplete"], "events": ["socket open"]}

console errors / page errors:
   none

result: OK
```

(`--only-live` runs just part 2 and 3; that is where the pasted live numbers come from.)

**b. Against the dev stub** on :8100 (standalone, no backend needed):

    uv run --python 3.12 --with playwright --with python-dotenv python web/check_web.py --live

```
1. mock replay (?mock=1)
   owner console signed in
   demo reset before the replay: HTTP 200
   mock session ses-6def6d080d
   beat 1: ARMORIQ BLOCK on screen (match_competitor_quote -> block)
   beat 2: ARMORIQ ALLOW on screen (issue_refund -> allow)
   beat 2: screen shows 'Refunded'
   beat 3: SHOP RULE on screen (issue_refund -> refused)
   beat 3: screen shows '…7QK2'
   beat 3 case card: serial on order …4MD9 vs serial on camera …7QK2 ✗
   beat 4: ARMORIQ BLOCK on screen (refund_to_account -> block)
   beat 4: screen shows 'already refunded'
   beat 5: ARMORIQ HOLD on screen (process_exchange -> hold)
   beat 5: screen shows 'Waiting for owner'
   owner sees the pending card with the still
   approve once -> waiting card flipped on pending_resolved
   beat 6: ARMORIQ BLOCK on screen (match_competitor_quote -> block)
   rails: 13 decisions on screen

2. live Gemini session (no mock)
   Gemini Live session open (ephemeral token accepted)
   audio arrived: yes 24 chunks, 4490 ms
   output transcript: yes 14 chunks
   tool call reached /api/tool: yes 1 new calls at the backend
   decision on the rail: yes

console errors / page errors:
   none

result: OK
```

Console errors: none in any run (page console + `pageerror` are hooked; the script prints whatever
it catches). The whole page log of each run is written next to the screenshots:
`web/shots/mock-page-log.txt` and `web/shots/live-page-log.txt`.

Live-mode findings:
- The browser opens the Live socket with the ephemeral token minted by `/api/session`:
  `gemini live connect: gemini-3.1-flash-live-preview (httpOptions.apiVersion=v1alpha, ephemeral token 76 chars)`
  (the page log records the token length only, never the value).
- apiVersion: the JS SDK only documents ephemeral tokens for `v1alpha` (it logs a warning for
  anything else) and the app pins `v1alpha`. Probe with fresh tokens: **both `v1alpha` and `v1beta`
  opened the socket and returned `setupComplete` today** (`web/shots/live-apiversion.txt`). Since the
  SDK's supported path is v1alpha, that is what ships.
- Typed turn → 24 kHz audio out, streamed output transcript, the model asked to see the item, the
  tile swap fired from that sentence, and `get_order` reached `/api/tool` → a real ArmorIQ decision
  on the rail with the case card filled from the `case` event.
- The waiting-card flip ran against the real backend: hold → pending card with the camera still
  (served by `GET /api/pending/{id}/still.jpg?token=…`) → *Approve once* → real ArmorIQ approval,
  MCP `process_exchange` executed → `pending_resolved` → green "Approved by the owner — exchanged
  · $170" on the call screen. After the run the backend's pending list showed `process_exchange
  executed` and zero waiting.
- One stale waiting delegation from an earlier test run was rejected before this run, so nothing was
  left dangling on the ArmorIQ side.

### Screenshots (`web/shots/`)
From the real-backend run: `mock-1-block.png` … `mock-6-owner-claim.png` (one per beat),
`mock-5b-approved.png` (waiting card flipped), `mock-7-final.png`; `owner-0-dashboard.png`,
`owner-1-pending.png`, `owner-2-approved.png` (console); `live-1-connected.png`, `live-2-turn.png`
(real session, fake camera device); plus the two page logs and `live-apiversion.txt`. The stub run
produces the same frames (it was inspected visually too).

### Known gaps
- The mock replay is scripted (drawn canvas as the camera, canned captions); it exercises the real
  `/api/tool` + SSE path but not Gemini. Beat 3's serial is drawn, not filmed off a real phone.
- The case-card logic lives in the stub here; against the real backend it renders whatever `case`
  events arrive.
- `?role=owner` on the call screen (owner identity in the browser) is wired but only smoke-tested
  through the stub.
- The page expects whoever serves `/api` to also serve `web/` (CONTRACTS §2 line 43). The instance
  that was up on :8000 did not, which is why `--real` carries its own tiny proxy for the checks.

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

## Beat 3 serial read (shot 3, 2026-10-17 00:43, dim room)
Real phone (iPhone 15, About screen, about 35 cm away). Full 1280×720 frame: both gemini-3.1-flash-lite and gemini-3.8-flash read `QWRQ446JQH`. The 768 px copy (the size of a live frame): `GWRQ4483QH` / `GWRQ4463QH`, so characters 7–8 drift. Order 1042's serial ends in `4MD9`, which shares nothing with any of these reads, so the mismatch refusal holds whatever the misread. Keep beat 3. For filming: brighter light, and the web app sends one full-resolution frame when it swaps to the camera tile.
Props for beats 2 and 5 need serial labels the agent can read that match the seed (`C65W-88120-8RT2` on the charger, `0457-2213-X7T1` on the controller). Otherwise an unreadable serial gets recorded and the serial rule refuses the refund or exchange.

## Slack live check (2026-10-17)
App "Counter · ArmorIQ" (A0C9ZMELAM7) created with the Slack CLI login on t3-agent-1 (`apps.manifest.create` + `apps.developerInstall`, which returns the bot and app-level tokens), channel `#counter-approvals` (C0C9RHLTXJ7). A $170 `process_exchange` hold posted a card; the owner tapped Approve once in Slack. The backend made one `/delegation/approve`, one re-check (`/iap/sdk/enforce`) and one MCP call. Pending `pen-50f1944292a3224f` → `executed`, approver from Slack. The `exchanges` table gained exactly one row (exc-2; exc-1 came from the browser check's console approval). Nothing waiting afterwards.

## finish

Branch `build/finish`, 2026-10-16 evening. This section is the last mile: Cloud Run deploy, owner login from the call screen, printed serial labels, README/DEMO/SUBMISSION, and one end-to-end pass on the deployed revision. Everything below is real output.

### Deployed revision

`bash deploy/deploy.sh`, with the fallback the script documents (the project's compute service account cannot grant `secretmanager.secretAccessor`; `secrets.setIamPolicy` answers 403, so the deploy carried an `--env-vars-file` with the same keys, outside the repo, deleted right after):

```
secrets: --env-vars-file /tmp/opencode/counter-env-vars.yaml (secret IAM grant failed on this project)
Building using Dockerfile and deploying container to Cloud Run service [counter] in project [gen-lang-client-0261050164] region [us-central1]
Building and deploying...
Validating configuration......done
Uploading sources....................done
Building Container [...] done
Setting IAM Policy........done
Creating Revision [...] done
Routing traffic.....done
Done.
Service [counter] revision [counter-00002-d5s] has been deployed and is serving 100 percent of traffic.
Service URL: https://counter-1027824348124.us-central1.run.app
```

`counter-00002-d5s` is the revision that logs the Slack card outcome (`slack: hold card updated for <id>: <outcome>`). One Slack listener at a time: Cloud Run owns the live service; every local run in this section had `SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN` blanked.

### Live checks on the deployed revision

HTTP:

```
$ for p in /api/healthz / /owner.html; do curl -s -o /dev/null -w "$p -> %{http_code}\n" \
    "https://counter-1027824348124.us-central1.run.app$p"; done
/api/healthz -> 200
/ -> 200
/owner.html -> 200
$ curl -s https://counter-1027824348124.us-central1.run.app/api/healthz
{"ok":true}
```

(`/healthz` without the `/api` prefix is the MCP server's route; it only listens inside the container.)

Six beats, `cd backend && COUNTER_BACKEND=https://counter-1027824348124.us-central1.run.app uv run python scripts/beats.py`:

```
remote backend: skipping the local MCP check (the MCP server runs inside https://counter-1027824348124.us-central1.run.app)
backend https://counter-1027824348124.us-central1.run.app · ArmorIQ https://api.armoriq.ai
reset: ok
sessions: counter counter@shop (ses-be0cf974) · owner owner · Sam (ses-7675fd31)

B1 unlocked phone + printed $420 quote: match_competitor_quote 420 -> ARMORIQ block
    match_competitor_quote $420 -> ARMORIQ / block / tool_not_in_allowlist / 500ms
  B1 PASS  expect ARMORIQ block | got ARMORIQ/block tool_not_in_allowlist (blocked_by_policy)

B2 $45 charger return, order 1101: issue_refund 45 -> allow + refunded
    issue_refund $45 -> ARMORIQ / allow / allow_with_logging / 655ms
  B2 PASS  expect ARMORIQ allow + refunded | got ARMORIQ/allow refunded -> Visa ending 6604

B3 wrong phone for order 1042 (serial ...7QK2 on camera): refund must be refused by shop rules
    record_inspection -> ARMORIQ / allow / allow_with_logging / 676ms
    issue_refund $799 -> SHOP_RULE / refused / serial on camera doesn't match the order / 0ms
  B3 PASS  expect inspection recorded, then SHOP_RULE refused (serial mismatch) | got inspection=recorded, refund SHOP_RULE/refused serial on camera doesn't match the order

B4 send it to my husband's card: refund_to_account 45 -> ARMORIQ block; repeat refund 45 -> already refunded
    refund_to_account $45 -> ARMORIQ / block / tool_not_in_allowlist / 539ms
    issue_refund $45 -> SHOP_RULE / refused / this order was already refunded / 0ms
  B4 PASS  expect refund_to_account ARMORIQ block + second refund SHOP_RULE refused | got refund_to_account ARMORIQ/block tool_not_in_allowlist; refund #2 SHOP_RULE/refused this order was already refunded

B5 upgrade, $170 gap: process_exchange 170 -> HOLD, owner approves in Slack/console -> executed once
    record_inspection -> ARMORIQ / allow / allow_with_logging / 543ms
    process_exchange $170 -> ARMORIQ / hold / amount_requires_approval / 1164ms
    approve pen-e68fc94efb0e9c5d -> 200 / executed / armoriq 201 / 852ms
  B5 PASS  expect HOLD then approve -> executed once (charged 170) | got hold hold deleg 6b7b7aed, approve -> executed

B6 same tool, same amount: counter match 420 -> block; owner match 420 -> allow
    match_competitor_quote $420 -> ARMORIQ / block / tool_not_in_allowlist / 494ms
    match_competitor_quote $420 -> ARMORIQ / allow / enforcement_passed / 538ms
  B6 PASS  expect counter block, owner allow | got counter block tool_not_in_allowlist; owner allow enforcement_passed -> matched

cleanup: reject delegations left pending
  (none left pending)

=== summary ===
B1       PASS  ARMORIQ block | got: ARMORIQ/block tool_not_in_allowlist (blocked_by_policy)
B2       PASS  ARMORIQ allow + refunded | got: ARMORIQ/allow refunded -> Visa ending 6604
B3       PASS  inspection recorded, then SHOP_RULE refused (serial mismatch) | got: inspection=recorded, refund SHOP_RULE/refused serial on camera doesn't match the order
B4       PASS  refund_to_account ARMORIQ block + second refund SHOP_RULE refused | got: refund_to_account ARMORIQ/block tool_not_in_allowlist; refund #2 SHOP_RULE/refused this order was already refunded
B5       PASS  HOLD then approve -> executed once (charged 170) | got: hold hold deleg 6b7b7aed, approve -> executed
B6       PASS  counter block, owner allow | got: counter block tool_not_in_allowlist; owner allow enforcement_passed -> matched
ALL BEATS PASS
exit=0
```

Headless Chrome with the live Gemini session, `uv run --python 3.12 --with playwright --with python-dotenv python web/check_web.py --base https://counter-1027824348124.us-central1.run.app --live`:

```
target https://counter-1027824348124.us-central1.run.app (web/ and /api from the same origin)

1. mock replay (?mock=1)
   owner console signed in
   demo reset before the replay: HTTP 200
   signed in again after the reset
   mock session ses-d4ceff736a52b158
   shot mock-1-block.png
   beat 1: ARMORIQ BLOCK on screen (match_competitor_quote -> block)
   shot mock-2-refund.png
   beat 2: ARMORIQ ALLOW on screen (issue_refund -> allow)
   beat 2: screen shows 'Refunded'
   shot mock-3-shop-rule.png
   beat 3: SHOP RULE on screen (issue_refund -> refused)
   beat 3: screen shows '…7QK2'
   beat 3 case card: serial on order …4MD9 vs serial on camera …7QK2 ✗
   shot mock-4-refund-to-account.png
   beat 4: ARMORIQ BLOCK on screen (refund_to_account -> block)
   beat 4: screen shows 'already refunded'
   shot mock-5-hold.png
   beat 5: ARMORIQ HOLD on screen (process_exchange -> hold)
   beat 5: screen shows 'Waiting for owner'
   owner sees the pending card with the still
   shot mock-5b-approved.png
   approve once -> waiting card flipped on pending_resolved
   shot mock-6-owner-claim.png
   beat 6: ARMORIQ BLOCK on screen (match_competitor_quote -> block)
   rails: 14 decisions on screen

1b. owner login from the call screen (/?role=owner)
   password form shown before any session
   wrong password: error on screen ('Wrong password.')
   wrong password: no session started
   shot owner-login-1-wrong.png
   owner badge on screen: owner · Sam
   beat 6 tool allowed in the owner session (match_competitor_quote 420 -> ARMORIQ allow -> matched)
   owner decision on the Decisions rail (ARMORIQ ALLOW)
   shot owner-login-2-allowed.png

2. live Gemini session (no mock)
   Gemini Live session open (ephemeral token accepted)
   shot live-1-connected.png
   typed turn sent: Hi, I want to return a charger, order 1101
   shot live-2-turn.png
   audio arrived: yes 20 chunks, 4040 ms
   output transcript: yes 10 chunks
   tool call reached /api/tool: yes 1 sent by the page, decisions came back (no /api/_dev_state)
   decision on the rail: yes 
   caption on screen: YouHi, I want to return a charger, order 1101 CounterI see the order for the 65W charger. Can you hold it up to the camera so I can check its condition?
   token probe: gemini-3.1-flash-live-preview (httpOptions.apiVersion=v1alpha, ephemeral token 76 chars)

3. token apiVersion probe
   token + v1alpha: {"version": "v1alpha", "setupComplete": true, "firstMessages": ["setupComplete"], "events": ["socket open"]}
   token + v1beta: {"version": "v1beta", "setupComplete": true, "firstMessages": ["setupComplete"], "events": ["socket open"]}
   ephemeral token + apiVersion: {"v1alpha": {"version": "v1alpha", "setupComplete": true, "firstMessages": ["setupComplete"], "events": ["socket open"]}, "v1beta": {"version": "v1beta", "setupComplete": true, "firstMessages": ["setupComplete"], "events": ["socket open"]}}

console errors / page errors:
   owner-login console.error: Failed to load resource: the server responded with a status of 401 ()

result: OK
exit=0
```

The only console error is the expected 401 from the wrong-password case.

### Hold posted to Slack, reject flips the card

Probe (`/tmp/opencode/reject_probe.py`, helper outside the repo): reset, counter session, inspection, `process_exchange 170` -> hold, `POST /api/pending/{id}/reject` with the owner token, then read `/api/pending` and reset again.

```
owner session ok: owner · Sam
reset: 200 {'ok': True}
owner session ok (after reset)
counter session ok: counter@shop ses-26d2f8b8
inspection: ARMORIQ/allow allow_with_logging
exchange: ARMORIQ/hold amount_requires_approval pending=pen-592cf539d7bb5238 deleg=f14fa7e6
reject: 200 status=rejected armoriq_reject=201
pending waiting now: 0
reset: 200 {'ok': True}
pending after reset: 0
REJECT PROBE OK
```

Cloud Run logs from the same run, `gcloud logging read 'resource.labels.service_name="counter" AND textPayload:"slack:"'`:

```
2026-10-16T21:51:01.223480Z  INFO counter.slack: slack: bridge enabled (channel C0C9RHLTXJ7)
2026-10-16T21:51:01.504798Z  INFO counter.slack: A new session (s_8777279189204) has been established
2026-10-16T21:58:04.870593Z  INFO counter.slack: slack: hold pen-592cf539d7bb5238 posted for process_exchange
2026-10-16T21:58:06.844317Z  INFO httpx: HTTP Request: POST https://api.armoriq.ai/delegation/reject "HTTP/1.1 201 Created"
2026-10-16T21:58:07.007174Z  INFO counter.slack: slack: hold card updated for pen-592cf539d7bb5238: Rejected by Owner
```

The Slack app's scopes cannot read channel history, so the card text is verified through `SlackBridge.update_hold`'s log line. The owner console stage card shows the same outcome through `pending_resolved`. One failed attempt on the way is on record: the first probe reused an owner token across a reset, the reject answered 401, and the leftover delegation was rejected in a cleanup pass (`reject -> rejected armoriq_reject_status 201`); nothing was left pending.

### Owner login from the call screen (local backend)

Same script against the local stack (`--base http://127.0.0.1:8000 --live`, real backend and real MCP on 8001, Slack env blanked):

```
target http://127.0.0.1:8000 (web/ and /api from the same origin)

1. mock replay (?mock=1)
   owner console signed in
   demo reset before the replay: HTTP 200
   signed in again after the reset
   mock session ses-336a6479650bfc82
   shot mock-1-block.png
   beat 1: ARMORIQ BLOCK on screen (match_competitor_quote -> block)
   shot mock-2-refund.png
   beat 2: ARMORIQ ALLOW on screen (issue_refund -> allow)
   beat 2: screen shows 'Refunded'
   shot mock-3-shop-rule.png
   beat 3: SHOP RULE on screen (issue_refund -> refused)
   beat 3: screen shows '…7QK2'
   beat 3 case card: serial on order …4MD9 vs serial on camera …7QK2 ✗
   shot mock-4-refund-to-account.png
   beat 4: ARMORIQ BLOCK on screen (refund_to_account -> block)
   beat 4: screen shows 'already refunded'
   shot mock-5-hold.png
   beat 5: ARMORIQ HOLD on screen (process_exchange -> hold)
   beat 5: screen shows 'Waiting for owner'
   owner sees the pending card with the still
   shot mock-5b-approved.png
   approve once -> waiting card flipped on pending_resolved
   shot mock-6-owner-claim.png
   beat 6: ARMORIQ BLOCK on screen (match_competitor_quote -> block)
   rails: 14 decisions on screen

1b. owner login from the call screen (/?role=owner)
   password form shown before any session
   wrong password: error on screen ('Wrong password.')
   wrong password: no session started
   shot owner-login-1-wrong.png
   owner badge on screen: owner · Sam
   beat 6 tool allowed in the owner session (match_competitor_quote 420 -> ARMORIQ allow -> matched)
   owner decision on the Decisions rail (ARMORIQ ALLOW)
   shot owner-login-2-allowed.png

2. live Gemini session (no mock)
   Gemini Live session open (ephemeral token accepted)
   shot live-1-connected.png
   typed turn sent: Hi, I want to return a charger, order 1101
   shot live-2-turn.png
   audio arrived: yes 24 chunks, 4160 ms
   output transcript: yes 12 chunks
   tool call reached /api/tool: yes 1 sent by the page, decisions came back (no /api/_dev_state)
   decision on the rail: yes 
   caption on screen: YouHi, I want to return a charger, order 1101 CounterSure, I can help with that. Can you hold up the charger and its box to the camera so I can look at the condition? I see it was purchased recently.
   token probe: gemini-3.1-flash-live-preview (httpOptions.apiVersion=v1alpha, ephemeral token 76 chars)

3. token apiVersion probe
   token + v1alpha: {"version": "v1alpha", "setupComplete": true, "firstMessages": ["setupComplete"], "events": ["socket open"]}
   token + v1beta: {"version": "v1beta", "setupComplete": true, "firstMessages": ["setupComplete"], "events": ["socket open"]}
   ephemeral token + apiVersion: {"v1alpha": {"version": "v1alpha", "setupComplete": true, "firstMessages": ["setupComplete"], "events": ["socket open"]}, "v1beta": {"version": "v1beta", "setupComplete": true, "firstMessages": ["setupComplete"], "events": ["socket open"]}}

console errors / page errors:
   owner-login console.error: Failed to load resource: the server responded with a status of 401 (Unauthorized)

result: OK
exit=0
```

Part 1b is new in `web/check_web.py` (`--base` target plus the owner-login section). It found one bug on the first run: the wait for `#ownerLogin[hidden]` used a visibility check that can never pass; it now waits on `element.hidden === true`. The older sections above show `rails: 13`; the count is 14 now, informational only.

### Serial label props

`props/labels.pdf` is an A4 sheet with one high-contrast monospace label per device (plus a spare of each): charger `C65W-88120-8RT2` (order 1101) and controller `0457-2213-X7T1` (order 1077). Rendered with headless chromium, then read back with `gemini-3.1-flash-lite`, including 768 px copies at live-frame size:

```
rendered props/labels.pdf and 4 label PNGs
lbl-charger-768.png: 768x325
lbl-controller-768.png: 768x323
lbl-charger-spare-768.png: 768x325
lbl-controller-spare-768.png: 768x323
labels-sheet-768.png: 494x768
reading with gemini-3.1-flash-lite

lbl-charger.png: PASS expected C65W-88120-8RT2
    raw: 'C65W-88120-8RT2'
lbl-controller.png: PASS expected 0457-2213-X7T1
    raw: '0457-2213-X7T1'
lbl-charger-spare.png: PASS expected C65W-88120-8RT2
    raw: 'C65W-88120-8RT2'
lbl-controller-spare.png: PASS expected 0457-2213-X7T1
    raw: '0457-2213-X7T1'
lbl-charger-768.png: PASS expected C65W-88120-8RT2
    raw: 'C65W-88120-8RT2'
lbl-controller-768.png: PASS expected 0457-2213-X7T1
    raw: '0457-2213-X7T1'
labels-sheet-768.png: PASS expected both serials
    raw: 'C65W-88120-8RT2\n0457-2213-X7T1\nC65W-88120-8RT2\n0457-2213-X7T1'

result: ALL READS EXACT
exit=0
```

### Tests

```
$ cd mcp_server && uv run pytest -q
9 passed in 4.98s

$ cd backend && uv run pytest -q
70 passed in 2.56s
```

### Docs

- `README.md`: what Counter is, the six beats and their deciding layer, the mermaid flow, ArmorIQ usage (SDK mode, one plan per session, variant B thresholds, approve flow), the four findings with quotes, run/test/deploy commands, screenshots.
- `DEMO.md`: props checklist, the exact customer line per beat with the expected rail row, shot list with timings, fire recovery.
- `SUBMISSION.md`: name and one-line pitch, a 210-word description, agent/MCP/ArmorIQ usage, links. Video link is a placeholder.

### Orchestrator check (after the DeepSeek run)

Re-ran the done conditions myself against the deployed service and fixed what I found.

- Secrets: the runtime service account now has `roles/secretmanager.secretAccessor` on all eight `counter-<key>` secrets (granted by the project owner from the Mac). `deploy/deploy.sh` deploys with `--set-secrets`; `gcloud run services describe` lists all eight keys as secret refs and only the five path/port vars as plain. Old revisions 00001 and 00002, which carried plain env values, are deleted.
- Slack race: a reject that lands before the hold card finishes posting was dropped, which left live buttons on a resolved card. `SlackBridge` now keeps that outcome and applies it right after the post. New test `test_outcome_before_the_card_lands_is_applied_after_posting`. Backend 71 passed, MCP 9 passed.
- Docs: README finding 3 now says the which-amount-runs question is untested (as FINDINGS.md does). Finding 4 no longer claims a re-plan after a decline; the app starts one plan per call session.

Revision `counter-00004-hjn`, fast reject probe:
```
healthz 200
record_inspection ARMORIQ allow allow_with_logging
process_exchange ARMORIQ hold amount_requires_approval
pending pen-49ec83d45f74579e
reject 200 rejected
reject again 409
waiting: []
reset 200
2026-10-16T22:20:23.225076Z	INFO counter.slack: slack: hold card updated for pen-49ec83d45f74579e: Rejected by Owner
2026-10-16T22:20:23.073782Z	INFO counter.slack: slack: hold pen-49ec83d45f74579e posted for process_exchange
2026-10-16T22:19:35.485056Z	INFO counter.slack: ⚡️ Bolt app is running!
2026-10-16T22:19:35.484733Z	INFO counter.slack: A new session (s_8732258353571) has been established
2026-10-16T22:19:35.220525Z	INFO counter.backend: slack bridge started
2026-10-16T22:19:35.220392Z	INFO counter.slack: slack: bridge enabled (channel C0C9RHLTXJ7)
```

Beats against revision 00004:
```
=== summary ===
B1       PASS  ARMORIQ block | got: ARMORIQ/block tool_not_in_allowlist (blocked_by_policy)
B2       PASS  ARMORIQ allow + refunded | got: ARMORIQ/allow refunded -> Visa ending 6604
B3       PASS  inspection recorded, then SHOP_RULE refused (serial mismatch) | got: inspection=recorded, refund SHOP_RULE/refused serial on camera doesn't match the order
B4       PASS  refund_to_account ARMORIQ block + second refund SHOP_RULE refused | got: refund_to_account ARMORIQ/block tool_not_in_allowlist; refund #2 SHOP_RULE/refused this order was already refunded
B5       PASS  HOLD then approve -> executed once (charged 170) | got: hold hold deleg d181114c, approve -> executed
B6       PASS  counter block, owner allow | got: counter block tool_not_in_allowlist; owner allow enforcement_passed -> matched
ALL BEATS PASS
```

`check_web.py --base <url> --live` against revision 00003: result OK (mock replay, owner login 1b, live Gemini turn with audio and a tool call). Only console error: the expected 401 from the wrong-password step.

### Left for the human

- Film the 90 s video from `DEMO.md`; `SUBMISSION.md` has the placeholder link.
- Glance at `#counter-approvals` to see the cards. The API cannot list them (scope), so the log line above is the machine-readable proof.
- No ArmorIQ delegations are pending from any run in this section (beats cleanup printed none; the probes rejected theirs).
