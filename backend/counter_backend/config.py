"""Settings for the Counter backend. Secrets come from ~/.config/counter/.env
(or COUNTER_ENV_FILE). Values are never logged."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(os.path.expanduser(os.environ.get("COUNTER_ENV_FILE", "~/.config/counter/.env")))

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent


@dataclass
class Settings:
    armor_api_key: str = field(default_factory=lambda: os.environ.get("ARMORIQ_API_KEY", ""))
    armor_login: str = field(default_factory=lambda: os.environ.get("ARMORIQ_LOGIN", ""))
    # ArmorIQ HTTP backend, used for /delegation/approve and /delegation/reject
    armor_backend: str = field(default_factory=lambda: os.environ.get("ARMORIQ_BACKEND", "https://api.armoriq.ai"))
    gemini_api_key: str = field(default_factory=lambda: os.environ.get("GEMINI_API_KEY", ""))
    owner_password: str = field(default_factory=lambda: os.environ.get("OWNER_PASSWORD", ""))
    mcp_token: str = field(default_factory=lambda: os.environ.get("MCP_TOKEN", ""))
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
