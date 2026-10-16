#!/usr/bin/env python
"""Drive Chrome headless against web/dev_stub.py and check the call screen.

    uv run --python 3.12 --with playwright --with python-dotenv python web/check_web.py [--live]

Part 1 (always): ?mock=1 replays the six beats; screenshots land in web/shots/.
Part 2 (always): owner console login, pending card, Approve once.
Part 3 (--live):   a real Gemini Live session: token, typed turn, audio, transcript,
                   tool call reaching /api/tool, and a v1beta-vs-v1alpha token probe.

Assumes the stub is already running on $CHECK_BASE (default http://127.0.0.1:8100).
Never prints secret values.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
SHOTS = HERE / "shots"
BASE = os.environ.get("CHECK_BASE", "http://127.0.0.1:8100")
ARGS = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
        "--autoplay-policy=no-user-gesture-required"]

load_dotenv(os.path.expanduser(os.environ.get("COUNTER_ENV_FILE", "~/.config/counter/.env")))

problems: list[str] = []
notes: list[str] = []


def note(msg: str):
    notes.append(msg)
    print("   " + msg, flush=True)


def fail(msg: str):
    problems.append(msg)
    print("  !" + msg, flush=True)


def http_json(path: str, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Owner-Token"] = token
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def dev_state():
    """The stub's counter endpoint; the real backend does not have it."""
    try:
        return http_json("/api/_dev_state")
    except urllib.error.HTTPError:
        return None


async def shot(page, name: str):
    SHOTS.mkdir(exist_ok=True)
    path = SHOTS / name
    await page.screenshot(path=str(path))
    print(f"   shot {path.name}", flush=True)


async def text_of(page, selector: str) -> str:
    try:
        return (await page.locator(selector).inner_text()).strip()
    except Exception:
        return ""


async def run_mock(pw, console_lines: list[str]):
    print("\n1. mock replay (?mock=1)", flush=True)
    browser = await pw.chromium.launch(args=ARGS)
    ctx = await browser.new_context(viewport={"width": 1920, "height": 1080})
    page = await ctx.new_page()
    page.on("pageerror", lambda e: console_lines.append(f"pageerror: {e}"))
    page.on("console", lambda m: console_lines.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)

    owner = await ctx.new_page()
    owner.on("pageerror", lambda e: console_lines.append(f"owner pageerror: {e}"))
    await owner.goto(BASE + "/owner.html", wait_until="domcontentloaded")
    await owner.fill("#pw", os.environ.get("OWNER_PASSWORD", ""))
    await owner.click("#go")
    await owner.wait_for_selector("#pendList", timeout=15000)
    await owner.wait_for_function("window.__owner && window.__owner.state.token !== ''", timeout=15000)
    note("owner console signed in")
    reset = await owner.evaluate("""async () => {
        const r = await fetch('api/reset', {method: 'POST', headers: {'X-Owner-Token': window.__owner.state.token}});
        return r.status;
    }""")
    note(f"demo reset before the replay: HTTP {reset}")
    await owner.reload(wait_until="domcontentloaded")
    await owner.wait_for_timeout(800)
    if await owner.is_visible("#login"):       # a reset clears sessions, so sign in again
        await owner.fill("#pw", os.environ.get("OWNER_PASSWORD", ""))
        await owner.click("#go")
        note("signed in again after the reset")
    await owner.wait_for_function("window.__owner && window.__owner.state.token !== ''", timeout=20000)
    await owner.screenshot(path=str(SHOTS / "owner-0-dashboard.png"))

    await page.goto(BASE + "/?mock=1&auto_approve=0", wait_until="domcontentloaded")
    await page.wait_for_function("window.__counter && window.__counter.state.sid", timeout=20000)
    note(f"mock session {await page.evaluate('window.__counter.state.sid')}")

    expectations = [
        # beat, file, chip, tool, action, extra text that must be on screen
        (0, "mock-1-block.png", "ARMORIQ BLOCK", "match_competitor_quote", "block", None),
        (1, "mock-2-refund.png", "ARMORIQ ALLOW", "issue_refund", "allow", "Refunded"),
        (2, "mock-3-shop-rule.png", "SHOP RULE", "issue_refund", "refused", "…7QK2"),
        (3, "mock-4-refund-to-account.png", "ARMORIQ BLOCK", "refund_to_account", "block", "already refunded"),
        (4, "mock-5-hold.png", "ARMORIQ HOLD", "process_exchange", "hold", "Waiting for owner"),
        (5, "mock-6-owner-claim.png", "ARMORIQ BLOCK", "match_competitor_quote", "block", None),
    ]
    for i, name, chip, tool, action, extra in expectations:
        try:
            await page.wait_for_function(
                f"window.__counter.state.beatIndex >= {i} && window.__counter.state.decisions.some("
                f"d => d.tool === {json.dumps(tool)} && d.action === {json.dumps(action)})", timeout=60000)
        except Exception:
            fail(f"beat {i + 1}: {tool} {action} never appeared")
            break
        await page.wait_for_timeout(1200)
        await shot(page, name)
        rail = await text_of(page, "#rail")
        if chip not in rail:
            fail(f"beat {i + 1}: {chip} not in the rail")
        else:
            note(f"beat {i + 1}: {chip} on screen ({tool} -> {action})")
        if extra:
            onscreen = (await text_of(page, "#caseCard")) + " " + rail
            if extra not in onscreen:
                fail(f"beat {i + 1}: expected {extra!r} on screen")
            else:
                note(f"beat {i + 1}: screen shows {extra!r}")
        if i == 2:
            case = await text_of(page, "#caseCard")
            missing = [w for w in ("Serial on order", "…4MD9", "Serial on camera", "✗") if w not in case]
            if missing:
                fail(f"beat 3 case card missing {missing}")
            else:
                note("beat 3 case card: serial on order …4MD9 vs serial on camera …7QK2 ✗")
        if i == 4:                                   # hold: owner card, then approve            await owner.wait_for_selector('.pend:not(.resolved)', timeout=30000)
            await owner.screenshot(path=str(SHOTS / "owner-1-pending.png"))
            note("owner sees the pending card with the still")
            still_ok = await owner.evaluate(
                """() => [...document.querySelectorAll('.pend img')].every(i => i.naturalWidth > 0)""")
            if not still_ok:
                fail("pending still did not load in the owner console")
            await owner.click(".pend:not(.resolved) .ap")
            await page.wait_for_function("window.__counter.state.pending === null", timeout=30000)
            await page.wait_for_timeout(700)
            await shot(page, "mock-5b-approved.png")
            await owner.screenshot(path=str(SHOTS / "owner-2-approved.png"))
            note("approve once -> waiting card flipped on pending_resolved")

    rail = await text_of(page, "#rail")
    for chip in ("ARMORIQ ALLOW", "ARMORIQ HOLD", "ARMORIQ BLOCK", "SHOP RULE"):
        if chip not in rail:
            fail(f"rail missing chip {chip}")
    note(f"rails: {len(await page.evaluate('window.__counter.state.decisions'))} decisions on screen")
    await page.screenshot(path=str(SHOTS / "mock-7-final.png"))

    # the page log, written next to the shots for the record
    logs = await page.evaluate("window.__counter.state.logs")
    (SHOTS / "mock-page-log.txt").write_text("\n".join(logs))
    await ctx.close()
    await browser.close()


async def run_live(pw, console_lines: list[str]):
    print("\n2. live Gemini session (no mock)", flush=True)
    browser = await pw.chromium.launch(args=ARGS)
    ctx = await browser.new_context(viewport={"width": 1920, "height": 1080},
                                    permissions=["microphone", "camera"])
    page = await ctx.new_page()
    page.on("pageerror", lambda e: console_lines.append(f"live pageerror: {e}"))
    page.on("console", lambda m: console_lines.append(f"live console.{m.type}: {m.text}") if m.type == "error" else None)

    before_state = dev_state()                     # the stub counts calls; the real backend has no such endpoint
    before = before_state["tool_calls"] if before_state else None
    await page.goto(BASE + "/", wait_until="domcontentloaded")
    try:
        await page.wait_for_function("window.__counter && window.__counter.state.connected === true", timeout=45000)
        note("Gemini Live session open (ephemeral token accepted)")
    except Exception:
        fail("Gemini Live session did not open")
        await shot(page, "live-failed.png")
        (SHOTS / "live-page-log.txt").write_text("\n".join(await page.evaluate("window.__counter.state.logs")))
        await ctx.close(); await browser.close()
        return
    await page.wait_for_timeout(1500)
    await shot(page, "live-1-connected.png")

    await page.fill("#textInput", "Hi, I want to return a charger, order 1101")
    await page.click("#textForm button.send")
    note("typed turn sent: Hi, I want to return a charger, order 1101")

    stats = {"audioOut": 0, "outTranscripts": 0, "inTranscripts": 0, "toolCalls": 0}
    sent = 0
    for _ in range(30):                                   # up to 90 s
        stats = await page.evaluate("window.__counter.state.stats")
        sent = stats["toolCalls"]
        after = dev_state()
        backend_new = (after["tool_calls"] - before) if (after is not None and before is not None) else None
        if stats["audioOut"] > 0 and stats["outTranscripts"] > 0 and (sent > 0 if backend_new is None else backend_new > 0):
            break
        await page.wait_for_timeout(3000)
    await page.wait_for_timeout(2500)
    await page.wait_for_timeout(800 if stats["audioOut"] else 0)
    await shot(page, "live-2-turn.png")
    after_state = dev_state()

    caption = await text_of(page, "#transcript")
    if after_state is not None and before is not None:
        tool_ok = after_state["tool_calls"] > before
        tool_detail = f"{after_state['tool_calls'] - before} new calls at the backend"
    else:
        tool_ok = sent > 0 and await page.evaluate("window.__counter.state.decisions.length") > 0
        tool_detail = f"{sent} sent by the page, decisions came back (no /api/_dev_state)"
    checks = [
        ("audio arrived", stats["audioOut"] > 0, f"{stats['audioOut']} chunks, {stats['audioMsOut']:.0f} ms"),
        ("output transcript", stats["outTranscripts"] > 0, f"{stats['outTranscripts']} chunks"),
        ("tool call reached /api/tool", tool_ok, tool_detail),
        ("decision on the rail", await page.evaluate("window.__counter.state.decisions.length") > 0, ""),
    ]
    for label, ok, extra in checks:
        (note if ok else fail)(f"{label}: {'yes' if ok else 'no'} {extra}")
    note("caption on screen: " + " ".join(caption.split())[:220])

    logs = await page.evaluate("window.__counter.state.logs")
    (SHOTS / "live-page-log.txt").write_text("\n".join(logs))
    api_version_line = next((l for l in logs if "apiVersion" in l), "")
    note("token probe: " + api_version_line.split("connect:")[-1].strip() if api_version_line else "token probe: n/a")

    print("\n3. token apiVersion probe", flush=True)
    probe = await run_probe(page)
    note("ephemeral token + apiVersion: " + json.dumps(probe))
    (SHOTS / "live-apiversion.txt").write_text(json.dumps(probe, indent=1))

    await ctx.close()
    await browser.close()


PROBE_JS = """async ([token, model, version]) => {
  const events = [];
  const msgs = [];
  let setup = false;
  try {
    const {GoogleGenAI} = await import("https://esm.run/@google/genai");
    const ai = new GoogleGenAI({apiKey: token, httpOptions: {apiVersion: version}});
    const session = await Promise.race([
      ai.live.connect({
        model, config: {responseModalities: ["AUDIO"]},
        callbacks: {
          onopen: () => events.push("socket open"),
          onmessage: (m) => { msgs.push(Object.keys(m).join(",")); if (m.setupComplete) setup = true; },
          onerror: (e) => events.push("error: " + String(e?.message || e)),
          onclose: (e) => events.push("closed: " + String(e?.reason || e?.code || e)),
        },
      }),
      new Promise((_, reject) => setTimeout(() => reject(new Error("no socket after 15 s")), 15000)),
    ]);
    const t0 = Date.now();                     // wait for the server to accept (or reject) the setup
    while (Date.now() - t0 < 8000 && !setup &&
           !events.some((e) => e.startsWith("closed") || e.startsWith("error"))) {
      await new Promise((r) => setTimeout(r, 200));
    }
    try { session.close(); } catch (e) { /* */ }
    return {version, setupComplete: setup, firstMessages: msgs.slice(0, 3), events: events.slice(0, 4)};
  } catch (e) {
    return {version, failed: String(e?.message || e).slice(0, 200), events: events.slice(0, 4)};
  }
}"""


async def probe_api_version(page, version: str) -> dict:
    """One fresh token (uses:1) per version, bounded by a timeout in the page."""
    sess = http_json("/api/session", {"role": "counter"})
    args = [sess["gemini"]["token"], sess["gemini"]["model"], version]
    try:
        return await asyncio.wait_for(page.evaluate(PROBE_JS, args), timeout=45)
    except asyncio.TimeoutError:
        return {"version": version, "failed": "page probe timed out after 45 s"}


async def run_probe(page) -> dict:
    out = {}
    for version in ("v1alpha", "v1beta"):
        out[version] = await probe_api_version(page, version)
        note(f"token + {version}: {json.dumps(out[version])}")
    return out


async def run_probe_only(pw, console_lines: list[str]):
    print("\n3. token apiVersion probe", flush=True)
    browser = await pw.chromium.launch(args=ARGS)
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto(BASE + "/owner.html", wait_until="domcontentloaded")   # no app boot, no token used
    probe = await run_probe(page)
    (SHOTS / "live-apiversion.txt").write_text(json.dumps(probe, indent=1))
    await ctx.close()
    await browser.close()


# ---------------------------------------------------------------- proxy mode
# The real backend serves web/ itself once wired; until then, --real PORT serves
# the files from this directory and streams /api/* to that backend, so the page
# and the API still share one origin in the browser.

class ProxyHandler(BaseHTTPRequestHandler):
    backend = "http://127.0.0.1:8000"
    root = HERE
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):        # keep the check output clean
        pass

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        if self.path.startswith("/api/"):
            return self.forward(method)
        if method != "GET":
            self.send_error(405)
            return
        path = self.path.split("?")[0]
        if path in ("", "/"):
            path = "/index.html"
        target = (self.root / path.lstrip("/")).resolve()
        if not str(target).startswith(str(self.root)) or not target.is_file():
            self.send_error(404)
            return
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
                 ".css": "text/css", ".png": "image/png", ".txt": "text/plain"}.get(target.suffix,
                                                                                       "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def forward(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(self.backend + self.path, data=body, method=method)
        for h in ("Content-Type", "X-Owner-Token", "Accept"):
            if self.headers.get(h):
                req.add_header(h, self.headers[h])
        try:
            resp = urllib.request.urlopen(req, timeout=900)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(resp.status)
        ctype = resp.headers.get("Content-Type") or "application/octet-stream"
        self.send_header("Content-Type", ctype)
        if resp.headers.get("Cache-Control"):
            self.send_header("Cache-Control", resp.headers["Cache-Control"])
        if "event-stream" in ctype:                       # SSE: stream until the client or upstream stops
            self.close_connection = True
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                while True:
                    chunk = resp.read1(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except Exception:
                pass
        else:
            data = resp.read()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)


def start_proxy(real_port: int, proxy_port: int):
    handler = type("BoundProxy", (ProxyHandler,), {"backend": f"http://127.0.0.1:{real_port}"})
    server = ThreadingHTTPServer(("127.0.0.1", proxy_port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


async def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="also run the live Gemini check")
    ap.add_argument("--only-live", action="store_true")
    ap.add_argument("--probe-only", action="store_true", help="only the token apiVersion probe")
    ap.add_argument("--real", type=int, metavar="PORT",
                    help="drive the page against the real backend on this port "
                         "(web/ is served from a local proxy that streams /api to it)")
    ap.add_argument("--proxy-port", type=int, default=8102)
    a = ap.parse_args()

    if a.real:
        start_proxy(a.real, a.proxy_port)
        BASE = f"http://127.0.0.1:{a.proxy_port}"
        print(f"proxy on {BASE} -> real backend http://127.0.0.1:{a.real}, files from {HERE}", flush=True)
        note("running against the real backend (no stub)")

    urlopen_ok = False
    try:
        urlopen_ok = urllib.request.urlopen(BASE + "/api/healthz", timeout=5).status == 200
    except Exception:
        urlopen_ok = False
    if not urlopen_ok:
        print(f"stub is not answering on {BASE}; start web/dev_stub.py first")
        return 2

    console_lines: list[str] = []
    async with async_playwright() as pw:
        if not a.only_live and not a.probe_only:
            await run_mock(pw, console_lines)
        if a.live or a.only_live:
            await run_live(pw, console_lines)
        if a.probe_only:
            await run_probe_only(pw, console_lines)

    print("\nconsole errors / page errors:", flush=True)
    interesting = [l for l in console_lines if not re.search(r"favicon|Failed to load resource.*404", l)]
    if interesting:
        for line in interesting[:20]:
            print("   " + line)
    else:
        print("   none")
    print("\nresult:", "FAIL" if problems else "OK", flush=True)
    if problems:
        for p in problems:
            print("   -", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
