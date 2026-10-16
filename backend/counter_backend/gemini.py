"""Gemini Live ephemeral tokens."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("counter.gemini")


def mint_ephemeral_token(api_key: str, api_version: str = "v1alpha", uses: int = 1,
                         expire_minutes: int = 30, new_session_minutes: int = 2) -> str:
    from google import genai

    client = genai.Client(api_key=api_key, http_options={"api_version": api_version})
    now = datetime.now(timezone.utc)
    cfg = {
        "uses": uses,
        "expire_time": now + timedelta(minutes=expire_minutes),
        "new_session_expire_time": now + timedelta(minutes=new_session_minutes),
    }
    tok = client.auth_tokens.create(config=cfg)
    return tok.name


async def mint_async(api_key: str, model: str, api_version: str = "v1alpha") -> dict:
    try:
        token = await asyncio.to_thread(mint_ephemeral_token, api_key, api_version)
        return {"token": token, "model": model}
    except Exception as e:
        log.warning("gemini token mint failed: %s", type(e).__name__)
        return {"token": None, "model": model, "error": f"{type(e).__name__}: {e}"}
