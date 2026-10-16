"""Throwaway probe helpers. Loads secrets into env, never prints them; redacts outputs."""
import json, os, re, time, sys
from pathlib import Path
from dotenv import load_dotenv
import httpx

load_dotenv("/Users/tayyabkhan/Downloads/.env")
MAIN_KEY = os.environ["ARMORIQ_API_KEY"]
_k2 = Path(__file__).parent / ".probe-org-key"
PROBE_KEY = _k2.read_text().strip() if _k2.exists() else ""
if os.environ.get("PROBE_ORG") == "2" and not PROBE_KEY:
    raise SystemExit("no probe-org key; run setup.py mint first")
API_KEY = PROBE_KEY if os.environ.get("PROBE_ORG") == "2" else MAIN_KEY
LOGIN = os.environ.get("ARMORIQ_LOGIN", "")
PASSWORD = os.environ.get("ARMORIQ_PASSWORD", "")
BACKEND = os.environ.get("BACKEND_ENDPOINT", "https://api.armoriq.ai")
HERE = Path(__file__).parent
LOG = HERE / "log.jsonl"
STATE = HERE / "state.json"

_SECRET_KEYS = re.compile(r"(token|jwt|signature|secret|password|api_?key|apikey|authorization|access|refresh|proof|merkle|public_?key|cookie)", re.I)
_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*")
_AK = re.compile(r"ak_(live|test|claw)_[A-Za-z0-9_\-]+")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def mask_email(e: str) -> str:
    if LOGIN and e.lower() == LOGIN.lower():
        return "<LOGIN_EMAIL>"
    local, _, dom = e.partition("@")
    return f"{local[:2]}***@{dom}"


def redact_str(s: str) -> str:
    for v in (MAIN_KEY, PROBE_KEY, PASSWORD):
        if v:
            s = s.replace(v, "<REDACTED>")
    s = _JWT.sub("<JWT>", s)
    s = _AK.sub("<APIKEY>", s)
    s = _EMAIL.sub(lambda m: mask_email(m.group(0)), s)
    return s


def redact(o, key=None):
    if isinstance(o, dict):
        return {k: redact(v, k) for k, v in o.items()}
    if isinstance(o, list):
        return [redact(v) for v in o]
    if isinstance(o, str):
        if key and _SECRET_KEYS.search(key) and len(o) > 12 and not key.lower().endswith(("id", "_at", "type", "status")):
            return f"<REDACTED len={len(o)}>"
        return redact_str(o)
    return o


def short(o, n=4000):
    s = json.dumps(redact(o), indent=1, default=str)
    return s if len(s) <= n else s[:n] + f"\n...<truncated {len(s)-n} chars>"


_jwt_cache = {}


def jwt():
    if "jwt" in _jwt_cache:
        return _jwt_cache["jwt"]
    r = httpx.post(f"{BACKEND}/auth/login", json={"email": LOGIN, "password": PASSWORD}, timeout=30)
    try:
        d = r.json()
    except Exception:
        d = {"text": r.text[:500]}
    tok = None
    def find(x):
        nonlocal tok
        if isinstance(x, dict):
            for k, v in x.items():
                if isinstance(v, str) and v.startswith("eyJ") and tok is None:
                    tok = v
                find(v)
        elif isinstance(x, list):
            for v in x: find(v)
    find(d)
    if tok is None:
        for c in r.cookies.jar:
            if c.value.startswith("eyJ"):
                tok = c.value
    _jwt_cache["jwt"] = tok
    _jwt_cache["login_resp"] = (r.status_code, d)
    return tok


def call(method, path, *, auth="key", json_body=None, params=None, headers=None, label="", quiet=False):
    h = {"Content-Type": "application/json"}
    if auth in ("key", "both"):
        h["X-API-Key"] = API_KEY
    if auth in ("jwt", "both"):
        t = jwt()
        if t:
            h["Authorization"] = f"Bearer {t}"
    if headers:
        h.update(headers)
    url = path if path.startswith("http") else BACKEND + path
    t0 = time.time()
    try:
        r = httpx.request(method, url, json=json_body, params=params, headers=h, timeout=30)
        status = r.status_code
        try:
            body = r.json()
        except Exception:
            body = r.text[:2000]
    except Exception as e:
        status, body = -1, f"EXC {e}"
    rec = {"ts": time.strftime("%H:%M:%S"), "label": label, "method": method, "path": path, "auth": auth,
           "params": params, "headers_extra": {k: v for k, v in (headers or {}).items()},
           "body": json_body, "status": status, "resp": body, "ms": int((time.time()-t0)*1000)}
    red = redact(rec)
    with LOG.open("a") as f:
        f.write(json.dumps(red, default=str) + "\n")
    if not quiet:
        print(f"== {label} {method} {path} [{auth}] params={redact(params)} -> {status}")
        if json_body is not None:
            print("   body:", short(json_body, 1500))
        print("   resp:", short(body))
    return status, body


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(**kw):
    s = load_state(); s.update(kw); STATE.write_text(json.dumps(s, indent=1))
