"""Settings for the Counter backend. Secrets come from ~/.config/counter/.env
(or COUNTER_ENV_FILE). Values are never logged."""
from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(os.path.expanduser(os.environ.get("COUNTER_ENV_FILE", "~/.config/counter/.env")))

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent


def public_mcp_token(mcp_token: str) -> str:
    """The public /mcp bearer, derived from MCP_TOKEN so Cloud Run needs no extra
    secret. Knowing it doesn't reveal MCP_TOKEN."""
    if not mcp_token:
        return ""
    return hmac.new(mcp_token.encode(), b"counter public mcp", hashlib.sha256).hexdigest()


@dataclass
class Settings:
    armor_api_key: str = field(default_factory=lambda: os.environ.get("ARMORIQ_API_KEY", ""))
    armor_login: str = field(default_factory=lambda: os.environ.get("ARMORIQ_LOGIN", ""))
    # ArmorIQ backend: SDK checks, telemetry, /delegation/approve and /delegation/reject.
    # The ArmorTools workspace (tools.armoriq.ai) lives on staging-api.armoriq.io.
    armor_backend: str = field(default_factory=lambda: os.environ.get(
        "ARMORIQ_BACKEND", os.environ.get("BACKEND_ENDPOINT", "https://api.armoriq.ai")))
    armor_iap: str = field(default_factory=lambda: os.environ.get("IAP_ENDPOINT", ""))
    armor_agent_id: str = field(default_factory=lambda: os.environ.get("ARMORIQ_AGENT_ID", "counter-front-desk"))
    # ship checks and tool runs to the ArmorIQ console (ARMORIQ_TELEMETRY=0 turns it off)
    armor_telemetry: bool = field(default_factory=lambda: os.environ.get("ARMORIQ_TELEMETRY", "1") not in ("0", "false", "off", "no"))
    gemini_api_key: str = field(default_factory=lambda: os.environ.get("GEMINI_API_KEY", ""))
    owner_password: str = field(default_factory=lambda: os.environ.get("OWNER_PASSWORD", ""))
    mcp_token: str = field(default_factory=lambda: os.environ.get("MCP_TOKEN", ""))
    # bearer for the public /mcp endpoint that ArmorIQ's MCP registry scans; empty = no endpoint
    mcp_public_token: str = field(default_factory=lambda: public_mcp_token(
        os.environ.get("MCP_TOKEN", "")) if os.environ.get("MCP_PUBLIC") == "1" else "")
    mcp_url: str = field(default_factory=lambda: os.environ.get("MCP_URL", "http://127.0.0.1:8001"))
    web_dir: Path = field(default_factory=lambda: Path(os.environ.get("COUNTER_WEB_DIR", "../web")))
    gemini_model: str = field(default_factory=lambda: os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-live-preview"))
    gemini_api_version: str = field(default_factory=lambda: os.environ.get("GEMINI_API_VERSION", "v1alpha"))
    slack_bot_token: str = field(default_factory=lambda: os.environ.get("SLACK_BOT_TOKEN", ""))
    slack_app_token: str = field(default_factory=lambda: os.environ.get("SLACK_APP_TOKEN", ""))
    slack_channel: str = field(default_factory=lambda: os.environ.get("SLACK_CHANNEL", ""))

    @property
    def web_path(self) -> Path:
        p = self.web_dir
        return p if p.is_absolute() else (BACKEND_DIR / p).resolve()

    @classmethod
    def from_env(cls) -> "Settings":
        return cls()
