# Counter: contracts between the three parts

Read `SPEC.md` first. This file fixes the interfaces so the MCP server + backend, the web app and the Slack bridge can be built in parallel. If you need to change a contract, write the change and the reason in `PROGRESS.md` under "Contract changes" instead of silently diverging.

## Repo layout (each part owns its directory; don't edit the others)
```
mcp_server/   FastMCP server + SQLite seed            (thread: core)
backend/      FastAPI gate, ArmorIQ, SSE, Gemini token (thread: core)
backend/counter_backend/slack_bridge.py + tests       (thread: slack)
web/          static call UI + owner console          (thread: web)
reference/    research code and evidence (read-only)
```
Python parts use `uv` with their own `pyproject.toml` (Python 3.12). Web is plain HTML/CSS/ES modules, no build step; `@google/genai` loaded from `https://esm.run/@google/genai` (or esm.sh).

## Secrets
All secrets live in `~/.config/counter/.env` (outside the repo, so every worktree sees it). Load with `python-dotenv` from `os.environ.get("COUNTER_ENV_FILE", "~/.config/counter/.env")` (expand `~`). Keys: `ARMORIQ_API_KEY` (probe-org key), `ARMORIQ_LOGIN` (the org admin email = the owner identity), `GEMINI_API_KEY`, `OWNER_PASSWORD`, `MCP_TOKEN`, later `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CHANNEL`.
**Never print, log, commit or echo a secret value or the owner email.** Mask emails in logs (`sa***@domain`). `.env*` is gitignored; `.env.example` lists names only.

## 1. MCP server (`mcp_server/`)
- FastMCP (`fastmcp` package), streamable HTTP at `http://127.0.0.1:8001/mcp`. Every request must carry `Authorization: Bearer $MCP_TOKEN` or get 401.
- SQLite at `$COUNTER_DB` (default `mcp_server/counter.db`), created and seeded on start if missing.
- Extra HTTP route `POST /reset` (same bearer) re-seeds the DB. `GET /healthz` → `{"ok": true}` (no auth).
- Seed data = `ORDERS` in `reference/gemini/common.py` (orders 1042, 1077, 1101, exactly those fields), plus a price guide: `iPhone 15 Pro 256GB` grade A 360 / grade B 310 / grade C 240; activation-locked → 0 with the note "activation-locked devices: $0 until the lock is removed". Unknown model → 0 with a note "not in price guide, page the owner".
- The 11 tools, names and arguments exactly as `TOOLS` in `reference/gemini/common.py` (same descriptions; the model was tested on them). Each returns a JSON object. Results:

| tool | result |
|---|---|
| lookup_price | `{model, condition, offer, note}` |
| get_order | the full order row, **including `serial`** (the backend strips it before the model sees it) or `{error:"order not found"}` |
| record_inspection | `{status:"recorded", inspection_id, order_id, serial_seen, condition}` (stored) |
| notify_customer_on_file | `{status:"sent", to:"contact on order <id>", template}` (stored) |
| page_owner | `{status:"paged", eta:"the owner will answer on their phone"}` (stored) |
| issue_payout | `{status:"paid", amount, customer_id, payout_id}` (stored) |
| issue_refund | `{status:"refunded", amount, to:"<order payment>", refund_id}` (stored) |
| process_exchange | `{status:"exchanged", order_id, new_item, charged:amount, exchange_id}` (stored) |
| match_competitor_quote | `{status:"matched", competitor, new_offer:amount}` (stored) |
| waive_lock_check | `{status:"waived", device}` (stored) |
| refund_to_account | `{status:"refunded", amount, to:account, refund_id}` (stored) |

The MCP server has **no** policy logic. It does what it's told; the backend is the only caller.

## 2. Backend (`backend/`, FastAPI, port 8000)
Serves `web/` as static files at `/` (path from `COUNTER_WEB_DIR`, default `../web` relative to `backend/`). JSON API under `/api`:

### `POST /api/session`
Body `{"role": "counter"}` or `{"role": "owner", "password": "..."}` (checked against `OWNER_PASSWORD`; wrong → 401).
- counter: identity = a fresh non-member email `counter-<8 hex>@example.com`, chosen by the server. Label `counter@shop`.
- owner: identity = `ARMORIQ_LOGIN`. Label `owner · Sam`.
- Creates an ArmorIQ session and plan for that identity: `ArmorIQClient(api_key=...).for_user(email).start_session(SessionOptions(mode="sdk", default_mcp_name="counter", validity_seconds=3600))`, then `start_plan([{"name": "counter__<tool>", "args": {...}} for all 11 tools], goal="counter front desk")`. Pattern: `reference/armoriq/sdkprobe.py` and `tests_lib.py`. Checks call `session.check("counter__"+tool, args, user_email=email)`.
- Mints a Gemini ephemeral token (see below).
- Returns `{"session_id", "role", "identity_label", "gemini": {"token", "model": "gemini-3.1-flash-live-preview"}, "system_prompt", "tools": [<11 function declarations as plain JSON, Gemini schema style: {"name","description","parameters":{"type":"OBJECT",...}}>]}`.
  System prompt = `reference/gemini/system_prompt.txt` plus one added line: "Call the tool before you tell the customer anything is done; never say a refund, payout or exchange happened unless a tool result says so."

Gemini token (python `google-genai`): `genai.Client(http_options={"api_version": "v1alpha"}).auth_tokens.create(config={"uses": 1, "expire_time": now+30min, "new_session_expire_time": now+2min})`; return `token.name`. The browser uses it as `new GoogleGenAI({apiKey: token, httpOptions: {apiVersion: "v1alpha"}})`. The docs disagree on v1alpha vs v1beta for connecting with a token: test both from a script and use the one that connects; write the answer in PROGRESS.md.

### `POST /api/tool`
Body `{"session_id", "call_id", "name", "args": {...}, "camera_on": bool, "still": "<base64 jpeg or null>"}`.
Returns `{"response": {...}, "decision": Decision}`. The browser sends `response` back to Gemini as the FunctionResponse, unchanged.

Order of checks (stop at the first that refuses):
1. Unknown tool → `{"status":"error","reason":"unknown tool"}`, layer `SHOP_RULE`.
2. **Shop rules** (layer `SHOP_RULE`, action `refused`; ArmorIQ is not asked):
   - money tools (`issue_payout, issue_refund, process_exchange, match_competitor_quote, refund_to_account`) with `amount` missing, non-numeric or ≤ 0 → reason "amount must be a positive number";
   - `issue_refund`/`process_exchange` on an order whose recorded `serial_seen` (from this session's `record_inspection`) doesn't end in the order's last 4 serial characters (compare uppercase alphanumerics only) → "serial on camera doesn't match the order";
   - `issue_refund` on an order that already had a successful refund (`issue_refund` or `refund_to_account`) (any session, since last reset) → "this order was already refunded";
   - `record_inspection`, `issue_refund`, `process_exchange`, `issue_payout` with `camera_on` false → "camera is off; I need to see the item";
   - Keep `refund_to_account`, `match_competitor_quote` and `waive_lock_check` out of the serial and refund rules on purpose: they must reach ArmorIQ so the video shows ArmorIQ blocking them.
   - a call to a tool that has a pending HOLD in this session → "waiting for the owner on an earlier request".
   Model response: `{"status":"refused_by_shop_rule","decided_by":"shop rules","reason":...}`.
3. **ArmorIQ** `session.check()`. Map the result:
   - allow → call the MCP tool, response = the tool result (for `get_order` remove `serial` and `serial_last4` and add `"serial_check":"done by the system when you record the inspection"`).
   - hold (delegation) → store a PendingAction, response `{"status":"held_for_approval","decided_by":"ArmorIQ","reason":"<armoriq reason>","owner_paged":true,"note":"Tell the customer the owner has to approve this; it is not done yet."}`.
   - block → `{"status":"blocked_by_policy","decided_by":"ArmorIQ","reason":"<armoriq reason>","note":"This account can't do this. Offer what you can do instead."}`.
   - ArmorIQ error/timeout → treat as block with reason "policy check unavailable" (fail closed).

`Decision` (also emitted on SSE): `{"id", "ts", "session_id", "role", "tool", "args", "layer": "SHOP_RULE"|"ARMORIQ", "action": "allow"|"hold"|"block"|"refused", "reason", "armoriq_reason"?, "ms", "delegation_id"?, "pending_id"?, "result"?}`. `args` shown in the console must not include the still.

### PendingAction and approvals
`{"pending_id", "session_id", "tool", "args", "still" (jpeg bytes, kept server-side), "delegation_id", "status": "waiting"|"approved"|"rejected"|"executed"|"failed", "created_ts", "customer_said"?}`.
- `GET /api/pending` (owner auth) → list; `GET /api/pending/{id}/still.jpg` (owner auth).
- `POST /api/pending/{id}/approve` and `/reject` (owner auth = header `X-Owner-Token` returned by an owner `/api/session`; the Slack bridge calls the same Python functions directly).
- approve = ArmorIQ `POST {BACKEND}/delegation/approve` body `{"delegationId", "approverEmail": ARMORIQ_LOGIN, "approverRole": "org_admin"}` with headers `X-API-Key` and `x-user-email: ARMORIQ_LOGIN` (see `reference/armoriq/tests_lib.py approve()`), then re-run `session.check()` on the **stored** tool+args (expect allow / approved_delegation), then execute the stored action on MCP once. Status → `executed` with the result, or `failed` with the reason.
- reject = `POST /delegation/reject` `{"delegationId","approverEmail","rejectionReason"}`. Status → `rejected`.
- Approving twice, or approving a non-waiting action, does nothing and returns 409.

### `GET /api/events?session_id=...` (SSE)
`session_id=*` with an owner token streams every session. Event types (JSON `data`):
- `decision` — a Decision.
- `case` — `{"session_id", "case": {...}}`: the case card, built only from tool results: `{"kind": "return"|"exchange"|"trade_in"|null, "order": {order_id, item, price_paid, payment, serial_last4}, "inspection": {serial_seen, condition, serial_match: bool|null}, "outcome": {"status": "refunded"|"exchanged"|"paid"|"waiting_for_owner"|"refused"|"blocked", "amount", "detail"}}`.
- `pending` — a PendingAction without the still (`has_still: bool`).
- `pending_resolved` — `{"pending_id", "session_id", "status", "tool", "result"?}`. The browser of that session, on `executed`, sends Gemini a text turn: `[Shop system: the owner approved. <tool> done: <result json>. Tell the customer.]`; on `rejected`: `[Shop system: the owner declined <tool>. Tell the customer.]`.
- `reset`.

### Other
- `POST /api/reset` (owner auth): MCP `/reset`, clear refunds-per-order, pending, sessions. `GET /api/healthz`.
- Every ArmorIQ check and MCP call is timed into `ms`.

## 3. Web (`web/`)
- `index.html` — the call (see `SPEC.md` "What the judges see", `reference/interface-sketch.html`, `reference/0*.png`). Query `?role=owner` shows a password field and starts an owner session.
- Flow: `POST /api/session` → connect Gemini Live with the token, model from the response, `config: {responseModalities: ["AUDIO"], systemInstruction, tools: [{functionDeclarations: tools}], outputAudioTranscription: {}, inputAudioTranscription: {}}` → mic as 16 kHz 16-bit PCM via AudioWorklet (`sendRealtimeInput({audio: {data, mimeType: "audio/pcm;rate=16000"}})`) → camera frames at 1 fps, JPEG, longest side 768 px (`sendRealtimeInput({video: {data, mimeType: "image/jpeg"}})`) only while the camera is on → play 24 kHz PCM audio out, with barge-in (stop playback on `serverContent.interrupted`).
- On `toolCall`: for each function call POST `/api/tool` (attach the latest full-resolution still for record_inspection, issue_refund, process_exchange, issue_payout, match_competitor_quote) and reply with `session.sendToolResponse({functionResponses: [{id, name, response}]})`.
- Tile swap: the customer camera takes the big tile when the camera is on and (a) the agent's output transcript contains one of "show me", "hold it up", "hold up", "let me see", "camera", "screen", or (b) the operator presses `S`. It goes back after 12 s with no tool call, or on `S` again. On the swap, send one full-resolution frame (up to 1920 px) as a video frame too.
- Console: case card from `case` events only; Decisions rail with two groups, ArmorIQ (green ALLOW / amber HOLD / red BLOCK) and Shop rules (grey), each row `chip · tool · amount · reason · ms`; a waiting card on stage while a HOLD is pending that flips to the result on `pending_resolved`.
- Top bar: camera toggle, mic toggle, "Protected by ArmorIQ · acting as <identity_label>" badge.
- Captions: the agent's output transcript and the customer's input transcript. A text box sends `sendClientContent({turns: text, turnComplete: true})` (typing works when the mic is off).
- `owner.html` — owner console: password login, live feed of all decisions (`session_id=*`), pending cards with the still, Approve once / Reject buttons, a Reset demo button.
- `?mock=1` — no Gemini: a scripted replay that POSTs the six beats' tool calls to `/api/tool` in order, so the console can be checked without a mic.

## 4. Slack bridge (`backend/counter_backend/slack_bridge.py`)
```python
class SlackBridge:
    def __init__(self, bot_token, app_token, channel, on_approve, on_reject): ...  # on_* are async (pending_id, approver_name) -> PendingAction|None
    async def start(self) -> None          # slack_bolt AsyncApp, Socket Mode, run as a background task; no-op if tokens missing
    async def post_hold(self, pending: dict, still_jpeg: bytes | None, customer_said: str | None) -> None
    async def post_block(self, decision: dict) -> None        # one-line message with the tool, amount and reason
    async def update_hold(self, pending: dict) -> None        # edit the card: "Approved by X · exchanged" / "Rejected by X"
```
Hold card (Block Kit): header "Counter needs approval", the still (upload with `files_upload_v2` to the channel thread, or as image block), fields Tool / Amount / Order / ArmorIQ reason / customer's words, buttons `Approve once` (style primary, action_id `counter_approve`, value = pending_id) and `Reject` (action_id `counter_reject`). After a click: replace the buttons with the outcome. Port the button/handler pattern from `reference/careplus/slack-bot.ts`. Manifest for the new app: `slack/manifest.yaml`.
The backend creates the bridge in its lifespan and calls `post_hold` / `post_block` / `update_hold`.
