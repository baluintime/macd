"""Configuration — where the Upstox credentials and runtime switches come from.

Precedence, highest first:

  1. real environment variables
  2. a .env file in the project root (see .env.example)
  3. the defaults below

Nothing here is ever rendered to the page or written to a log; the settings
object exposes masked accessors for display and keeps the raw secret private.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Optional

DEFAULT_ENV_FILE = Path(".env")

# Endpoint hosts are configurable because Upstox versions them independently:
# market data sits on the main API host, order placement on the HFT host.
DEFAULT_API_BASE = "https://api.upstox.com"
DEFAULT_HFT_BASE = "https://api-hft.upstox.com"
DEFAULT_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"

TRUTHY = {"1", "true", "yes", "y", "on", "enabled"}


def parse_env_file(path: Path) -> Dict[str, str]:
    """Minimal KEY=VALUE reader — avoids a python-dotenv dependency.

    Ignores blank lines and #comments, strips one layer of matching quotes, and
    tolerates `export KEY=value`.
    """
    values: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return values

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def mask(secret: Optional[str], keep: int = 4) -> str:
    """Show enough of a credential to recognise it, never enough to use it."""
    if not secret:
        return ""
    if len(secret) <= keep * 2:
        return "•" * len(secret)
    return f"{secret[:keep]}{'•' * 6}{secret[-keep:]}"


@dataclass(frozen=True)
class UpstoxSettings:
    """Everything needed to reach Upstox. Secrets stay in this object."""

    api_key: str = ""
    api_secret: str = ""
    redirect_uri: str = ""
    api_base: str = DEFAULT_API_BASE
    hft_base: str = DEFAULT_HFT_BASE
    instruments_url: str = DEFAULT_INSTRUMENTS_URL
    token_file: Path = Path(".upstox-token.json")
    # Placing real orders takes a deliberate opt-in, separate from having keys.
    live_trading_enabled: bool = False
    timeout_seconds: float = 10.0

    @property
    def configured(self) -> bool:
        """True when an OAuth login can actually be started."""
        return bool(self.api_key and self.api_secret and self.redirect_uri)

    @property
    def missing(self):
        return [name for name, value in (
            ("UPSTOX_API_KEY", self.api_key),
            ("UPSTOX_API_SECRET", self.api_secret),
            ("UPSTOX_REDIRECT_URI", self.redirect_uri),
        ) if not value]

    def public(self) -> Dict[str, object]:
        """A safe-to-render view — masked key, secret reduced to present/absent."""
        return {
            "apiKey": mask(self.api_key),
            "apiSecretSet": bool(self.api_secret),
            "redirectUri": self.redirect_uri,
            "apiBase": self.api_base,
            "hftBase": self.hft_base,
            "tokenFile": str(self.token_file),
            "liveTradingEnabled": self.live_trading_enabled,
            "configured": self.configured,
            "missing": self.missing,
        }


@dataclass(frozen=True)
class Settings:
    upstox: UpstoxSettings = field(default_factory=UpstoxSettings)
    state_file: Path = Path("desk-state.json")
    env_file_loaded: Optional[Path] = None


def _flag(source: Mapping[str, str], key: str, default: bool = False) -> bool:
    raw = source.get(key)
    return raw.strip().lower() in TRUTHY if raw is not None else default


def load_settings(env: Optional[Mapping[str, str]] = None,
                  env_file: Path = DEFAULT_ENV_FILE) -> Settings:
    """Build settings from the process environment, layered over a .env file."""
    env = os.environ if env is None else env
    from_file = parse_env_file(Path(env_file))
    merged: Dict[str, str] = {**from_file, **{k: v for k, v in env.items() if v != ""}}

    def get(key: str, default: str = "") -> str:
        return str(merged.get(key, default)).strip()

    upstox = UpstoxSettings(
        api_key=get("UPSTOX_API_KEY"),
        api_secret=get("UPSTOX_API_SECRET"),
        redirect_uri=get("UPSTOX_REDIRECT_URI"),
        api_base=get("UPSTOX_API_BASE", DEFAULT_API_BASE).rstrip("/"),
        hft_base=get("UPSTOX_HFT_BASE", DEFAULT_HFT_BASE).rstrip("/"),
        instruments_url=get("UPSTOX_INSTRUMENTS_URL", DEFAULT_INSTRUMENTS_URL),
        token_file=Path(get("UPSTOX_TOKEN_FILE", ".upstox-token.json")),
        live_trading_enabled=_flag(merged, "UPSTOX_LIVE_TRADING"),
        timeout_seconds=float(get("UPSTOX_TIMEOUT_SECONDS", "10") or 10),
    )

    return Settings(
        upstox=upstox,
        state_file=Path(get("MACD_DESK_STATE", "desk-state.json")),
        env_file_loaded=Path(env_file) if from_file else None,
    )
