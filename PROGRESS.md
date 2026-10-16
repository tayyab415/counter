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
