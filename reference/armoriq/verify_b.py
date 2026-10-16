"""Variant B check: blocked tools block at 170 for a non-member; money tool tiers; owner allow."""
import time
from tests_lib import *
who = f"counter-vb-{int(time.time())}@example.com"
s = session_for(who)
for tool, a, exp in [("match_competitor_quote", {"competitor": "GadgetCash", "amount": 170}, "block"),
                     ("refund_to_account", {"order_id": "1101", "amount": 170, "account": "x"}, "block"),
                     ("waive_lock_check", {"device": "x", "reason": "y", "amount": 170}, "block"),
                     ("match_competitor_quote", {"competitor": "GadgetCash", "amount": 420}, "block"),
                     ("issue_refund", {"order_id": "1101", "amount": 45}, "allow"),
                     ("lookup_price", {"model": "x", "condition": "y"}, "allow"),
                     ("process_exchange", {"order_id": "1077", "amount": 170}, "hold")]:
    chk(s, "counter", who, "VB", tool, a, (exp,))
reject_all()
so = session_for(LOGIN)
chk(so, "owner", LOGIN, "VB", "match_competitor_quote", {"competitor": "GadgetCash", "amount": 420}, ("allow",))
