"""Counter backend: FastAPI gate, SSE, pending approvals, static web.

`create_app` accepts injected ArmorIQ / MCP / Slack / token-minter implementations
so tests run without network. The production app builds the real ones from env.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import gemini
from .armor import ArmorIQLayer
from .config import Settings
from .gate import Gate
from .mcp_client import MCPToolClient
from .store import CounterSession, Store, new_id, pending_view
from .tools import TOOL_DECLS, system_prompt

log = logging.getLogger("counter.backend")
OWNER_LABEL = "owner \u00b7 Sam"


class SessionBody(BaseModel):
    role: str
    password: str | None = None


class ToolBody(BaseModel):
    session_id: str
    name: str
    args: dict = Field(default_factory=dict)
    camera_on: bool = False
    call_id: str | None = None
    still: str | None = None
    customer_said: str | None = None


class UsageBody(BaseModel):
    session_id: str
    input_tokens: int = Field(default=0, ge=0, le=2_000_000)
    output_tokens: int = Field(default=0, ge=0, le=2_000_000)
    cached_tokens: int = Field(default=0, ge=0, le=2_000_000)


def create_app(*, settings: Settings | None = None, armor=None, mcp=None, slack=None,
               token_minter=None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store()
    if armor is None:
        armor = ArmorIQLayer(settings.armor_api_key, settings.armor_login, settings.armor_backend,
                             iap=settings.armor_iap, agent_id=settings.armor_agent_id,
                             telemetry=settings.armor_telemetry)
    if mcp is None:
        mcp = MCPToolClient(settings.mcp_url, settings.mcp_token)
    gate = Gate(settings, store, armor, mcp, slack=slack)

    async def default_minter() -> dict:
        return await gemini.mint_async(settings.gemini_api_key, settings.gemini_model,
                                       settings.gemini_api_version)

    minter = token_minter or default_minter

    async def on_approve(pending_id: str, approver_name: str = "Slack"):
        return await gate.approve(pending_id, approver_name)

    async def on_reject(pending_id: str, approver_name: str = "Slack"):
        return await gate.reject(pending_id, approver_name)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if gate.slack is None:
            # Slack bridge lives on another branch; import lazily and only if configured.
            try:
                from .slack_bridge import SlackBridge
            except ImportError:
                SlackBridge = None
            if SlackBridge is not None and settings.slack_bot_token and settings.slack_app_token:
                try:
                    bridge = SlackBridge(bot_token=settings.slack_bot_token,
                                         app_token=settings.slack_app_token,
                                         channel=settings.slack_channel,
                                         on_approve=on_approve, on_reject=on_reject)
                    await bridge.start()
                    gate.slack = bridge
                    log.info("slack bridge started")
                except Exception as e:
                    log.warning("slack bridge not started: %s", type(e).__name__)
            else:
                log.info("slack bridge not configured; running without Slack")
        yield
        bridge = gate.slack
        if bridge is not None and hasattr(bridge, "stop"):
            try:
                await bridge.stop()
            except Exception:
                pass

    app = FastAPI(title="Counter", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.gate = gate

    # ------------------------------------------------------------ owner auth
    def require_owner(request: Request) -> str:
        token = request.headers.get("x-owner-token") or request.query_params.get("token")
        if not token or token not in store.owner_tokens:
            raise HTTPException(status_code=401, detail="owner token required")
        return token

    # ------------------------------------------------------------ session
    @app.post("/api/session")
    async def api_session(body: SessionBody):
        role = (body.role or "").strip().lower()
        if role == "counter":
            identity = f"counter-{secrets.token_hex(4)}@example.com"
            label = "counter@shop"
            owner_token = None
        elif role == "owner":
            if not settings.owner_password or body.password != settings.owner_password:
                raise HTTPException(status_code=401, detail="wrong password")
            identity = settings.armor_login
            label = OWNER_LABEL
            owner_token = secrets.token_urlsafe(32)
        else:
            raise HTTPException(status_code=400, detail="role must be 'counter' or 'owner'")
        try:
            armor_session = await armor.start_session(identity)
        except Exception as e:
            log.warning("ArmorIQ session start failed: %s", type(e).__name__)
            raise HTTPException(status_code=502,
                                detail=f"could not start ArmorIQ session: {type(e).__name__}")
        session = CounterSession(session_id=new_id("ses"), role=role, identity=identity,
                                 identity_label=label, owner_token=owner_token,
                                 armor_session=armor_session)
        store.sessions[session.session_id] = session
        if owner_token:
            store.owner_tokens.add(owner_token)
        gem = minter()
        if hasattr(gem, "__await__"):
            gem = await gem
        if not isinstance(gem, dict):
            gem = {"token": gem, "model": settings.gemini_model}
        payload = {"session_id": session.session_id, "role": role, "identity_label": label,
                   "gemini": gem, "system_prompt": system_prompt(), "tools": TOOL_DECLS}
        if owner_token:
            payload["owner_token"] = owner_token
        return payload

    # ------------------------------------------------------------ tool call
    @app.post("/api/tool")
    async def api_tool(body: ToolBody):
        session = store.sessions.get(body.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown session")
        return await gate.handle_tool(session, body.name, body.args, body.camera_on,
                                      body.still, body.call_id, body.customer_said)

    # ------------------------------------------------------------ model usage
    @app.post("/api/usage")
    async def api_usage(body: UsageBody):
        """Gemini token counts for one turn, recorded on the ArmorIQ session."""
        session = store.sessions.get(body.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown session")
        gate.record_usage(session, body.input_tokens, body.output_tokens, body.cached_tokens)
        return {"ok": True}

    # ------------------------------------------------------------ pending
    @app.get("/api/pending")
    async def api_pending(_owner: str = Depends(require_owner)):
        return [pending_view(p) for p in store.pending.values()]

    @app.get("/api/pending/{pending_id}/still.jpg")
    async def api_pending_still(pending_id: str, _owner: str = Depends(require_owner)):
        p = store.pending.get(pending_id)
        if p is None or not p.get("still"):
            raise HTTPException(status_code=404, detail="no still for this pending action")
        return Response(content=p["still"], media_type="image/jpeg")

    @app.post("/api/pending/{pending_id}/approve")
    async def api_pending_approve(pending_id: str, _owner: str = Depends(require_owner)):
        p = await gate.approve(pending_id)
        if p is None:
            raise HTTPException(status_code=409, detail="this action is not waiting")
        return pending_view(p)

    @app.post("/api/pending/{pending_id}/reject")
    async def api_pending_reject(pending_id: str, _owner: str = Depends(require_owner)):
        p = await gate.reject(pending_id)
        if p is None:
            raise HTTPException(status_code=409, detail="this action is not waiting")
        return pending_view(p)

    # ------------------------------------------------------------ events
    @app.get("/api/events")
    async def api_events(request: Request, session_id: str, token: str | None = None):
        if session_id == "*":
            header_token = request.headers.get("x-owner-token")
            if not ((token and token in store.owner_tokens)
                    or (header_token and header_token in store.owner_tokens)):
                raise HTTPException(status_code=401, detail="owner token required for session_id=*")
            key = "*"
        else:
            if session_id not in store.sessions:
                raise HTTPException(status_code=404, detail="unknown session")
            key = session_id
        queue = store.subscribe(key)

        async def gen():
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield f"event: {item['type']}\ndata: {json.dumps(item['data'], default=str)}\n\n"
            finally:
                store.unsubscribe(key, queue)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                          "Connection": "keep-alive"})

    # ------------------------------------------------------------ reset / health
    @app.post("/api/reset")
    async def api_reset(_owner: str = Depends(require_owner)):
        ok = await gate.reset()
        return {"ok": ok}

    @app.get("/api/healthz")
    async def api_healthz():
        return {"ok": True}

    # ------------------------------------------------------------ static web
    web_path = settings.web_path
    if web_path.is_dir():
        app.mount("/", StaticFiles(directory=str(web_path), html=True), name="web")
    else:
        log.warning("web dir not found at %s; serving API only", web_path)

    return app


def server_port() -> int:
    """Cloud Run sets $PORT; local dev falls back to 8000."""
    return int(os.environ.get("PORT", "8000"))


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=server_port(), log_level="info")


if __name__ == "__main__":
    main()
