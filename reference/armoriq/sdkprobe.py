"""SDK-mode probe: start_plan + session.check() with raw backend traffic logged (redacted)."""
import json, sys, time
import lib
from lib import *
from armoriq_sdk import ArmorIQClient
from armoriq_sdk.session import SessionOptions

RAW = []


def make_client(**kw):
    c = ArmorIQClient(api_key=API_KEY, **kw)
    orig_post, orig_get = c.http_client.post, c.http_client.get

    def wrap(fn, method):
        def inner(url, *a, **k):
            r = fn(url, *a, **k)
            try:
                body = r.json()
            except Exception:
                body = r.text[:1500]
            rec = {"method": method, "url": url.replace(BACKEND, ""), "req": k.get("json"), "params": k.get("params"),
                   "status": r.status_code, "resp": body}
            RAW.append(rec)
            with LOG.open("a") as f:
                f.write(json.dumps(redact({"label": "sdk-raw", **rec}), default=str) + "\n")
            return r
        return inner
    c.http_client.post = wrap(orig_post, "POST")
    c.http_client.get = wrap(orig_get, "GET")
    return c


def run(tool_calls, checks, mcp="probe-pay", user_email=None, goal="probe", client=None, show_raw=True, obs=True, scoped=True, show_mint_raw=False):
    user_email = user_email or LOGIN
    c = client or make_client()
    opts = SessionOptions(mode="sdk", default_mcp_name=mcp, validity_seconds=600)
    if not obs:
        from armoriq_sdk.observability import ObservabilityConfig
        opts.observability = ObservabilityConfig(enabled=False)
    s = c.for_user(user_email).start_session(opts) if scoped else c.start_session(opts)
    RAW.clear()
    out = {"plan": tool_calls, "mint": None, "checks": []}
    try:
        tok = s.start_plan(tool_calls, goal=goal)
        out["mint"] = {"plan_id": tok.plan_id, "policy_validation": tok.policy_validation,
                       "policy_snapshot": tok.policy_snapshot}
        print("MINT ok plan_id", tok.plan_id)
        print("  policy_validation:", short(tok.policy_validation, 1500))
        print("  policy_snapshot:", short(tok.policy_snapshot, 2500))
        if show_mint_raw:
            for r in RAW:
                print("  mint-raw", r["method"], r["url"], r["status"], "req:", short({k:v for k,v in (r["req"] or {}).items() if k!="plan"}, 600))
    except Exception as e:
        out["mint"] = {"error": f"{type(e).__name__}: {e}"}
        print("MINT FAILED", redact_str(f"{type(e).__name__}: {e}"))
        for r in RAW:
            print("  raw", r["method"], r["url"], r["status"], short(r["resp"], 1500))
        return out, s, c
    for name, args in checks:
        RAW.clear()
        r = s.check(name, args, user_email=user_email)
        d = {"tool": name, "args": args, "allowed": r.allowed, "action": r.action, "reason": r.reason,
             "delegation_id": r.delegation_id, "matched_policy": r.matched_policy, "obligations": r.obligations,
             "raw": [x for x in RAW]}
        out["checks"].append(d)
        print(f"CHECK {name} {args} -> allowed={r.allowed} action={r.action} deleg={r.delegation_id} policy={r.matched_policy}")
        print("   reason:", redact_str(str(r.reason)))
        if show_raw:
            for x in RAW:
                req = dict(x["req"] or {})
                for k in ("intent_token", "policy_snapshot"):
                    if k in req: req[k] = "<omitted>"
                print("   raw", x["method"], x["url"], x["params"] or "", "->", x["status"])
                print("     req:", short(req, 800))
                print("     resp:", short(x["resp"], 1500))
    try:
        s.flush_observability()
    except Exception:
        pass
    return out, s, c


if __name__ == "__main__":
    pass
