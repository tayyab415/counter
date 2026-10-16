# Counter — submission

**Name:** Counter
**One-line pitch:** The front desk of a repair shop, run by an AI agent on a video call, where every action the agent takes is checked by ArmorIQ before it happens.

## Description

Counter is the front desk of a phone and electronics repair shop, run by an AI agent on a video call. A customer talks to the counter screen and holds devices up to the camera; the agent looks up orders, reads serials, refunds, exchanges and trade-ins. The agent is a language model, so Counter trusts it the way a shop trusts a new employee, which is never. The browser can only reach tools through the backend gate, and it is not trusted either.

Shop rules run first and refuse what they must: a serial on camera that doesn't match the order, a second refund on the same order, an amount of zero, no camera. Everything that passes goes to ArmorIQ `session.check` in SDK mode, one plan per session with all 11 tools declared. ArmorIQ decides: allow up to $100, hold from $100.01 to $350, block above $350, and three tools the counter may never use at any amount. Holds become cards in Slack with the camera still; the owner taps Approve once, the stored action re-checks and runs exactly once. The same tool at the same amount is blocked for the counter and allowed for the owner, who signs in with a password. Every decision lands on a visible rail.

## How it works

- **MCP server:** 11 tools over a SQLite seed (orders 1042, 1077, 1101 and a price guide), FastMCP streamable HTTP with bearer auth. The only caller is the backend.
- **Agent:** Gemini Live (`gemini-3.1-flash-live-preview`). Audio and 1 fps camera frames run in the browser; tool calls POST to `/api/tool` on the backend gate.
- **Backend gate (FastAPI, Cloud Run):** shop rules, the ArmorIQ check, then the real MCP call. Also the pending-approval store, the SSE feed for the console, and the Slack bot over Socket Mode.
- **ArmorIQ:** `armoriq-sdk==0.6.10`, `SessionOptions(mode="sdk")`, policy `counter-policy` variant B. Counter sessions run as a fresh non-member email; the owner runs as the admin login. A HOLD stores the exact tool, args, still and delegation id; approve calls `POST /delegation/approve`, re-checks the stored action, and executes it once. A rejection is final for the plan, so one plan covers one customer conversation.

## What we found building against ArmorIQ

1. The amount check runs before `toolEnforcement`: a blocked tool called with an amount of 170 becomes an approvable hold (`hold / amount_requires_approval`, then `allow / approved_delegation` after approval). We use variant B so blocked tools answer `block / tool_not_in_allowlist` at any amount.
2. `approverRole` is a client-supplied field in the approve request body (`reference/armoriq/tests_lib.py:56`).
3. Approval dedup is per requester and tool, and ignores the amount: a second hold on the same tool gets the first hold's delegation id. The backend stores the exact action and re-checks it on approve.
4. A rejection sticks to the plan: the same tool answers `block / delegation_rejected` at any amount above the threshold until a new plan.

Full write-up with raw outputs: `reference/armoriq/FINDINGS.md`, `reference/armoriq/out_*.txt`.

## Links

- Repo: https://github.com/tayyab415/counter
- Live demo: https://counter-1027824348124.us-central1.run.app (no login; open `/?role=owner` for the owner login)
- Video (90 s): TODO — pending filming, see `DEMO.md` for the shot list
- All verified output: `PROGRESS.md`, section "finish"
