"""Access-token storage.

An Upstox access token is valid until 3:30 AM IST the following day, whatever
time it was issued — there is no refresh token, so the operator re-authorises
once a day. This module caches the token on disk (owner-readable only) and
knows when it has gone stale.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional

try:  # pragma: no cover - exercised implicitly by whichever branch runs
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # tzdata missing on a minimal image
    IST = timezone(timedelta(hours=5, minutes=30), "IST")

TOKEN_CUTOFF = time(3, 30)  # tokens die at 03:30 IST, daily


def expiry_after(issued_at: datetime) -> datetime:
    """The next 03:30 IST strictly after `issued_at`."""
    local = issued_at.astimezone(IST)
    cutoff = local.replace(hour=TOKEN_CUTOFF.hour, minute=TOKEN_CUTOFF.minute,
                           second=0, microsecond=0)
    if local >= cutoff:
        cutoff += timedelta(days=1)
    return cutoff


@dataclass
class Token:
    access_token: str
    issued_at: datetime
    expires_at: datetime
    user_id: str = ""
    user_name: str = ""
    email: str = ""

    @classmethod
    def issued_now(cls, access_token: str, profile: Optional[dict] = None,
                   now: Optional[datetime] = None) -> "Token":
        now = now or datetime.now(timezone.utc)
        profile = profile or {}
        return cls(
            access_token=access_token,
            issued_at=now,
            expires_at=expiry_after(now),
            user_id=str(profile.get("user_id", "") or ""),
            user_name=str(profile.get("user_name", "") or ""),
            email=str(profile.get("email", "") or ""),
        )

    def is_valid(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return bool(self.access_token) and now < self.expires_at

    def public(self, now: Optional[datetime] = None) -> dict:
        """Safe to render — never includes the token itself."""
        now = now or datetime.now(timezone.utc)
        return {
            "connected": self.is_valid(now),
            "userName": self.user_name,
            "userId": self.user_id,
            "issuedAt": self.issued_at.astimezone(IST).strftime("%d %b %H:%M"),
            "expiresAt": self.expires_at.astimezone(IST).strftime("%d %b %H:%M IST"),
        }

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "user_id": self.user_id,
            "user_name": self.user_name,
            "email": self.email,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Token":
        return cls(
            access_token=str(raw.get("access_token", "")),
            issued_at=datetime.fromisoformat(raw["issued_at"]),
            expires_at=datetime.fromisoformat(raw["expires_at"]),
            user_id=str(raw.get("user_id", "")),
            user_name=str(raw.get("user_name", "")),
            email=str(raw.get("email", "")),
        )


class TokenStore:
    """Reads and writes the cached token, owner-only on disk."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Optional[Token]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return Token.from_dict(json.load(handle))
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            return None

    def save(self, token: Token) -> None:
        if self.path.parent and not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # Create with 0600 from the outset — never widen after writing a secret.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(token.to_dict(), handle, indent=2)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def valid_token(self, now: Optional[datetime] = None) -> Optional[Token]:
        token = self.load()
        return token if token and token.is_valid(now) else None
