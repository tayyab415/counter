# ArmorIQ multi-tool test for Counter (prod, 2026-10-16 ~18:35-18:45 UTC)

Backend `https://api.armoriq.ai`, `armoriq-sdk` 0.6.10, `SessionOptions(mode="sdk")`, org `probe-org` (`61ed3f02-0a66-42d2-8ba8-31f656a417f3`) only.
Scripts, redacted request log (`log.jsonl`), per-check results (`results.jsonl`) and run outputs (`out_*.txt`) are in `research/r5/armoriq/`.
Secrets came from `/Users/tayyabkhan/Downloads/.env` only. `<LOGIN_EMAIL>` is the org admin's login. Non-members are fresh `counter-*-<ts>@example.com` emails.

Each identity ran one plan declaring all 11 tools once (`counter__<tool>`). The mint succeeded every time with `allowed_tools` = 11 and `denied_tools` = []. Every check reported `matchedPolicy: counter-policy`.

## Summary

| # | Question | Result |
|---|---|---|
| 1 | One shared amountThreshold applies to issue_payout, issue_refund and process_exchange independently | PASS |
| 1b | Delegation dedup across tools | PASS (no cross-tool dedup; same-tool dedup confirmed) |
| 2 | 5 read/notify tools allow; 3 toolEnforcement tools block | PASS as asked (see 2b) |
| 2b | toolEnforcement block holds up when the call carries an `amount` | **FAIL**: 101-350 turns the block into an approvable hold |
| 3 | Edges: -50 allowed; 100 allow / 100.5 and 101 hold; 350 hold / 350.01 and 351 block | PASS |
| 3b | A rejected hold does not stop later holds | **FAIL**: rejection makes that tool block for the rest of the plan |
| 4 | Admin gets plain allow on blocked tools and over-max amounts | PASS |
| 5 | hold → approve → allow once → hold again | PASS |
| 6 | Latency | allow 816 ms, hold 1817 ms, block 830 ms (medians) |

## Policy used (left active)

`counter-policy`, id `40cc3094-ac0c-492f-b3c1-bd05ba6aeb71`, imported with `POST /policies/yaml/import-apikey` (`onConflict: replace`), then activated with a JWT. The YAML body was this JSON:

```json
{"apiVersion": "armor.io/v1", "kind": "Policy",
 "metadata": {"name": "counter-policy", "orgId": "61ed3f02-0a66-42d2-8ba8-31f656a417f3",
              "targetType": "agent", "targetId": "0a2b03fd-f218-4742-b44f-e4338aada00c"},
 "spec": {"priority": 100, "defaultEnforcementAction": "block", "memberRules": {
   "*": {"allowedTools": ["lookup_price","get_order","record_inspection","notify_customer_on_file","page_owner",
                          "issue_payout","issue_refund","process_exchange","match_competitor_quote","waive_lock_check","refund_to_account"],
         "enforcementAction": "allow_log",
         "toolEnforcement": {"match_competitor_quote": "block", "waive_lock_check": "block", "refund_to_account": "block"},
         "amountThreshold": {"requireApprovalAbove": 100, "maxPerTransaction": 350, "currency": "USD"}},
   "<LOGIN_EMAIL>": {"allowedTools": ["<same 11>"], "enforcementAction": "allow"}}}}
```

The backend stored the email key as the admin's membershipId `484789a1-…`. Stored copy: `armoriq/policy_stored.json`.

I deactivated `probe-refund-policy` (`e37c885c-…`), so counter-policy is the only active policy in probe-org (`GET /policies?orgId=<org>`; the `x-organization-id` header does not scope that list, but the `orgId` query param does). `probe-mcp-policy` was already inactive.

## Results

### T1. Amount tiers on each money tool (non-member A, one plan, no rejects between tools)

| call | enforcementAction | reason |
|---|---|---|
| issue_payout 45 / 170 / 520 | allow / hold / block | allow_with_logging / amount_requires_approval / amount_exceeds_max |
| issue_refund 45 / 170 / 520 | allow / hold / block | same |
| process_exchange 45 / 170 / 520 | allow / hold / block | same |
| process_exchange 200 while its 170 hold is pending | hold | `/delegation/request` → "already pending approval", **same id** as the 170 hold |

The three 170 holds got three different delegation ids, so dedup is keyed per tool, not per requester. A second hold on the same tool reuses the pending id even at a different amount. After this step I rejected all three holds.

### T2. Read/notify and blocked tools (non-member A)

| call | enforcementAction | reason |
|---|---|---|
| lookup_price, get_order, record_inspection, notify_customer_on_file, page_owner | allow | allow_with_logging |
| match_competitor_quote amount 420 | block | **amount_exceeds_max** (not rule_action_block) |
| waive_lock_check (no amount) | block | rule_action_block |
| refund_to_account 45 | block | rule_action_block |

### T2b / G1. toolEnforcement block vs amountThreshold (non-members D, E)

| call | enforcementAction | reason |
|---|---|---|
| match_competitor_quote, no amount / 45 | block | rule_action_block |
| match_competitor_quote 170 | **hold** | amount_requires_approval |
| refund_to_account 170 | **hold** | amount_requires_approval |
| refund_to_account 520 | block | amount_exceeds_max |
| waive_lock_check amount 170 | **hold** | amount_requires_approval |
| G1: match_competitor_quote 170 → approve as admin → re-check | **allow** | approved_delegation |
| G1: waive_lock_check 170 → approve → re-check | **allow** | approved_delegation |

The order is: over-max blocks first, then over-threshold holds, and only then does toolEnforcement apply. A "blocked" tool called with an `amount` between 100 and 350 becomes a normal hold, and approving it lets the tool run.

**Fix that works (variant B, tested then rolled back):** drop the 3 tools from `"*".allowedTools` and remove `toolEnforcement`. Non-member F then got `block tool_not_in_allowlist` for match_competitor_quote (no amount and 170), refund_to_account (45 and 170), and waive_lock_check 170. issue_payout 45/170 still allowed/held, and lookup_price allowed. The mint still succeeded with an 11-tool plan (allowed_tools 8, denied_tools []), so the checks reach the backend and get logged. The admin still got allow on all of them. File: `armoriq/policy_variant_B.json`. **I restored the spec policy (A) as asked; switch to B if you want the block to hold.**

### T3. Edges on process_exchange

| call | enforcementAction | reason |
|---|---|---|
| -50 | allow | allow_with_logging (negative amounts pass) |
| 0 | allow | allow_with_logging |
| 100 | allow | allow_with_logging (threshold is strictly "above") |
| 100.5, 101 | hold | amount_requires_approval |
| 350 | hold | amount_requires_approval (max is inclusive) |
| 350.01, 351 | block | amount_exceeds_max |
| "170" (string) | hold | amount_requires_approval |

The first T3 pass reused non-member A after I had rejected A's process_exchange hold in T1. 100.5, 101, 350 and 170 all came back `block delegation_rejected`. The rows above for 100.5, 101, 350, 350.01 and "170" come from F2, which used a fresh email per amount.

### F1. Rejection stickiness (non-member C)

| step | enforcementAction | reason |
|---|---|---|
| process_exchange 170 | hold | delegation created, then rejected |
| same plan: process_exchange 170 | block | delegation_rejected ("Approval was rejected (delegation 3d3aebf2-…)", metadata has the planId) |
| same plan: process_exchange 120 | block | delegation_rejected (amount doesn't matter) |
| same plan: process_exchange 90 | allow | allow_with_logging (under threshold is unaffected) |
| same plan: issue_refund 170 | hold | other tools unaffected |
| new plan, same email: process_exchange 170 | hold | a new plan clears it |
| first plan again after 90 s | block | delegation_rejected (still sticky) |

### T4. Admin (`<LOGIN_EMAIL>`)

| call | enforcementAction | reason |
|---|---|---|
| match_competitor_quote 420 | allow | enforcement_passed |
| refund_to_account 45 | allow | enforcement_passed |
| issue_payout 520 | allow | enforcement_passed |
| waive_lock_check | allow | enforcement_passed |

### T5. Approve flow (non-member B, process_exchange 170)

| step | result |
|---|---|
| check | hold, delegation `9179b77d-…` |
| `POST /delegation/approve` as admin, `approverRole: org_admin` | 201 "Delegation approved" |
| re-check, same args | allow / approved_delegation; status becomes `executed` |
| re-check again | hold, new delegation `01799e7f-…` (rejected afterwards) |

### T6. Latency (all 67 checks, from this Mac)

| outcome | n | median | range |
|---|---|---|---|
| allow | 23 | 816 ms | 744-999 |
| hold (enforce + delegation request) | 21 | 1817 ms | 1667-3326 |
| block | 23 | 830 ms | 782-1433 |

## Surprises

1. **Approval unlocks a toolEnforcement-blocked tool** if the call has `amount` in (100, 350]. `waive_lock_check` with `amount: 170` was held, approved and allowed. This is a real bypass: a prompt-injected agent can add `amount` to a blocked call and ask for approval. Report it to ArmorIQ.
2. **A rejection blocks that tool for the requester for the rest of the plan**, at any amount above the threshold, and it lasted at least 90 s. A new plan clears it.
3. Delegation dedup is per requester and tool, and ignores the amount. A second pending hold on the same tool returns the first id, so approving it most likely approves the first hold's amount (my inference; not tested).
4. Negative amounts pass (`-50` allowed), as in r4.
5. `GET /policies` with a JWT ignores `x-organization-id` but honors `?orgId=`.

## Resources

- Created: policy `counter-policy` `40cc3094-ac0c-492f-b3c1-bd05ba6aeb71` (**active**, spec A).
- Changed: `probe-refund-policy` `e37c885c-eed3-405a-b472-c8d2c2eef4dd` → **inactive**.
- Reused, not created: agent `probe-refund-agent` `0a2b03fd-f218-4742-b44f-e4338aada00c`, which is the policy's target. The target is ignored at enforce time, but don't delete this agent without retargeting the policy.
- API key `cli-2026-10-16` `8e6e58cc-4f62-4ca3-8843-b16d2941a679`, minted with `POST /iap/sdk/switch-org`: **revoked** (status `revoked`), and the local key file is deleted. The revoke loop also re-sent revoke for the four older r4 keys (already revoked, 201). The main-org key still works.
- Delegations: 20 created. All of them are rejected or executed; 0 pending. Main org untouched.

## What this means for the build

1. The money rule works as one shared `"*"` rule: allow ≤100, hold 100.01-350, block >350, for all three money tools independently. The admin bypasses it by having its own key.
2. Use variant B (blocked tools left out of `"*".allowedTools`) instead of `toolEnforcement: block`, or strip `amount` from those three tools in the app. Otherwise an `amount` on a blocked tool makes it approvable.
3. After the owner rejects a hold, that tool stays blocked in the plan. Show "owner said no" and start a new plan (new session) per customer conversation, not per call.
4. Track one pending delegation per tool per customer in the app. Same-tool holds dedup to the first id, so store the exact action and amount you asked about (r4 Q10 still applies).
5. Budget about 0.8 s per allow or block and about 1.8 s per hold. Reject negative or zero amounts in the app, because ArmorIQ allows them.
