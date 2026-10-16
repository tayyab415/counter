#!/usr/bin/env python
"""Check the Gemini Live path end to end.

1. Call /api/session on the backend and take the ephemeral token it mints.
2. Open a Live session with that token + `gemini-3.1-flash-live-preview`,
   once with api_version v1alpha and once with v1beta (the token allows one
   new session, so each attempt gets its own token from /api/session).
3. Send one text turn, count audio bytes coming back, print a result table.

Usage: uv run python scripts/live_check.py        (from backend/, with dev.sh up)
Exit code 0 when at least one api version worked and audio came back.
"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.expanduser(os.environ.get("COUNTER_ENV_FILE", "~/.config/counter/.env")))

BACKEND = os.environ.get("COUNTER_BACKEND", "http://127.0.0.1:8000").rstrip("/")
TURN_TIMEOUT = float(os.environ.get("LIVE_TURN_TIMEOUT", "30"))
PROMPT = "Say one short sentence: what can you help me with?"


def get_token() -> tuple[str, str]:
    r = httpx.post(f"{BACKEND}/api/session", json={"role": "counter"}, timeout=60)
    r.raise_for_status()
    gem = r.json()["gemini"]
    if not gem.get("token"):
        raise SystemExit(f"backend returned no token: {gem.get('error')}")
    return gem["token"], gem["model"]


async def try_live(api_version: str, token: str, model: str) -> dict:
    from google import genai
    from google.genai import types

    out = {"api_version": api_version, "connect": False, "audio_bytes": 0,
           "text": "", "error": ""}
    client = genai.Client(api_key=token, http_options={"api_version": api_version})
    cfg = types.LiveConnectConfig(response_modalities=["AUDIO"])
    try:
        async with client.aio.live.connect(model=model, config=cfg) as ses:
            out["connect"] = True
            await ses.send_realtime_input(text=PROMPT)
            async with asyncio.timeout(TURN_TIMEOUT):
                async for msg in ses.receive():
                    sc = msg.server_content
                    if sc and sc.model_turn:
                        for part in sc.model_turn.parts or []:
                            if part.inline_data and part.inline_data.data:
                                out["audio_bytes"] += len(part.inline_data.data)
                    if sc and sc.output_transcription and sc.output_transcription.text:
                        out["text"] += sc.output_transcription.text
                    if sc and sc.turn_complete:
                        break
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"[:400]
    return out


async def main() -> int:
    rows = []
    for ver in ("v1alpha", "v1beta"):
        token, model = get_token()
        row = await try_live(ver, token, model)
        rows.append(row)
        status = "connect ok" if row["connect"] else "connect failed"
        print(f"{ver:8s} {status:14s} audio {row['audio_bytes']}B  text {row['text'][:60]!r}"
              + (f"  error {row['error']}" if row["error"] else ""))

    print("\n=== result ===")
    ok = [r for r in rows if r["connect"] and r["audio_bytes"] > 0]
    for r in rows:
        print(f"{r['api_version']:8s} connect={r['connect']} audio_bytes={r['audio_bytes']} "
              f"text={r['text'][:80]!r}")
    if ok:
        print(f"WORKING apiVersion with an ephemeral token: {', '.join(r['api_version'] for r in ok)}")
        print("audio came back: yes")
        return 0
    any_connect = [r for r in rows if r["connect"]]
    print("WORKING apiVersion with an ephemeral token: " + (", ".join(r["api_version"] for r in any_connect) or "none"))
    print("audio came back: no")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
