"""Counter policy setup in probe-org. Usage:
  python setup.py mint            # mint probe-org API key via switch-org (main key), stored 0600, never printed
  PROBE_ORG=2 python setup.py list
  PROBE_ORG=2 python setup.py import   # import counter-policy, activate, deactivate probe-refund-policy
  python setup.py revoke          # revoke minted cli-* keys in probe-org, delete local key file
"""
import sys, os, json
from pathlib import Path
from lib import *

ORG = "61ed3f02-0a66-42d2-8ba8-31f656a417f3"
OH = {"x-organization-id": ORG}
S = load_state()
TOOLS = ["lookup_price", "get_order", "record_inspection", "notify_customer_on_file", "page_owner",
         "issue_payout", "issue_refund", "process_exchange", "match_competitor_quote", "waive_lock_check",
         "refund_to_account"]

def policy_doc():
    return {"apiVersion": "armor.io/v1", "kind": "Policy",
            "metadata": {"name": "counter-policy", "orgId": ORG, "targetType": "agent", "targetId": S["agent_id"]},
            "spec": {"priority": 100, "defaultEnforcementAction": "block", "memberRules": {
                "*": {"allowedTools": TOOLS, "enforcementAction": "allow_log",
                      "toolEnforcement": {"match_competitor_quote": "block", "waive_lock_check": "block", "refund_to_account": "block"},
                      "amountThreshold": {"requireApprovalAbove": 100, "maxPerTransaction": 350, "currency": "USD"}},
                LOGIN.lower(): {"allowedTools": TOOLS, "enforcementAction": "allow"}}}}

def list_policies():
    st, b = call("GET", "/policies", auth="jwt", params={"orgId": ORG}, label="list", quiet=True)
    rows = [(x["policyId"], x["name"], x["status"], x.get("priority"), x.get("orgId")) for x in b.get("data", [])]
    for r in rows: print("  ", *r)
    return rows

cmd = sys.argv[1] if __name__ == "__main__" else None
if cmd == "mint":
    r = httpx.post(f"{BACKEND}/iap/sdk/switch-org", json={"org": "probe-org"}, headers={"X-API-Key": MAIN_KEY}, timeout=20)
    d = r.json(); assert d.get("org_id") == ORG, ("wrong org", r.status_code, redact(d))
    k = HERE / ".probe-org-key"; k.write_text(d["api_key"]); os.chmod(k, 0o600)
    print("minted", r.status_code, "org", d.get("org_id"))
elif cmd == "list":
    list_policies()
elif cmd == "import":
    st, b = call("POST", "/iap/sdk/bootstrap", json_body={}, label="bootstrap", quiet=True)
    print("key org check:", st, short(b, 400).replace("\n", " ")[:400])
    print("before:"); list_policies()
    d = policy_doc()
    st, b = call("POST", "/policies/yaml/import-apikey", json_body={"yaml": json.dumps(d), "onConflict": "replace"}, label="import-counter", quiet=True)
    print("import", st, short(b, 600))
    pid = b["results"][0]["policyId"]; save_state(counter_policy_id=pid)
    st, b = call("POST", f"/policies/{pid}/activate", auth="jwt", headers=OH, json_body={}, label="activate-counter", quiet=True)
    print("activate", st, b.get("message"))
    if S["old_policy_id"] != pid:
        st, b = call("POST", f"/policies/{S['old_policy_id']}/deactivate", auth="jwt", headers=OH, json_body={}, label="deactivate-old", quiet=True)
        print("deactivate probe-refund-policy", st, b.get("message"))
    print("after:"); list_policies()
    st, b = call("GET", f"/policies/{pid}", auth="jwt", headers=OH, label="get-counter", quiet=True)
    dd = b["data"]
    print("stored:", dd["status"], dd["priority"], dd["defaultEnforcementAction"], "memberRules keys:",
          ["<LOGIN_EMAIL>" if k == LOGIN.lower() else k for k in dd["memberRules"]])
    (HERE / "policy_stored.json").write_text(json.dumps(redact(dd), indent=1))
elif cmd == "revoke":
    st, b = call("GET", "/api-keys", auth="jwt", headers=OH, quiet=True, label="keys")
    for x in b.get("data", []):
        if x.get("orgId") == ORG and x.get("name", "").startswith("cli-") and not x.get("revokedAt") and x.get("isActive", True):
            s2, _ = call("POST", f"/api-keys/{x['apiKeyId']}/revoke", auth="jwt", headers=OH, json_body={}, quiet=True, label="revoke")
            print("revoke", x["apiKeyId"], s2)
    (HERE / ".probe-org-key").unlink(missing_ok=True)
    s3, _ = call("POST", "/iap/sdk/bootstrap", json_body={}, quiet=True, label="main-bootstrap")
    print("main key still works:", s3)
