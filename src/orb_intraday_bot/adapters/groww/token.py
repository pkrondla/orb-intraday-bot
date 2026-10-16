"""Access-token management for Groww, using the TOTP flow only.

How it works:
- The bot holds a TOTP token and TOTP secret. It generates the 6-digit code itself and exchanges
  token + code for a daily access token. No manual approval or pasting is needed.
- The access token is cached on disk and reused until the daily expiry boundary, so restarts do
  not fetch a new one. [Assumption] Groww access tokens expire daily at 06:00 IST. If that is
  wrong, change EXPIRY_HOUR_IST. An auth error also clears the cache.
- A 429 on token exchange pauses further token fetches. The cooldown is persisted to disk, so a
  crash loop that restarts the process cannot keep hammering the endpoint (lesson from the
  live bot).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from orb_intraday_bot.core.clock import IST

EXPIRY_HOUR_IST = 6
_RATE_LIMIT_MARKERS = ("rate limit", "ratelimit", "too many requests", "429")


class TokenProvider(Protocol):
    def get_token(self) -> str: ...

    def invalidate(self) -> None: ...


class TokenCooldownError(RuntimeError):
    """Raised instead of calling the token endpoint while a rate-limit cooldown is active."""


def is_rate_limit_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _RATE_LIMIT_MARKERS)


def _last_expiry_boundary(now: float) -> float:
    local = datetime.fromtimestamp(now, IST)
    boundary = local.replace(hour=EXPIRY_HOUR_IST, minute=0, second=0, microsecond=0)
    if local < boundary:
        boundary -= timedelta(days=1)
    return boundary.timestamp()


def extract_token(response: object) -> str:
    """The SDK documents a dict but the live bot passes the result straight through as a string.
    Accept both so either behaviour works."""
    if isinstance(response, str) and response:
        return response
    if isinstance(response, dict):
        for key in ("token", "access_token", "accessToken"):
            value = response.get(key)
            if isinstance(value, str) and value:
                return value
        payload = response.get("payload")
        if isinstance(payload, dict):
            return extract_token(payload)
    raise RuntimeError("Could not find an access token in the Groww token response")


def totp_fetcher(totp_token: str, totp_secret: str) -> str:
    """Generate the 6-digit code from the TOTP secret and exchange it for an access token."""
    import pyotp  # imported lazily so tests need no network or SDK calls
    from growwapi import GrowwAPI

    code = pyotp.TOTP(totp_secret).now()
    return extract_token(GrowwAPI.get_access_token(api_key=totp_token, totp=code))


class TokenManager:
    def __init__(
        self,
        totp_token: str,
        totp_secret: str,
        cache_path: Path,
        cooldown_sec: int = 180,
        fetcher: Callable[[str, str], str] = totp_fetcher,
        now: Callable[[], float] = time.time,
    ) -> None:
        if not totp_token or not totp_secret:
            raise ValueError("GROWW_TOTP_TOKEN and GROWW_TOTP_SECRET must both be set")
        self._totp_token = totp_token
        self._totp_secret = totp_secret
        self._cache_path = cache_path
        self._cooldown_sec = cooldown_sec
        self._fetcher = fetcher
        self._now = now

    # -- cache ---------------------------------------------------------------------------
    def _read(self) -> dict[str, float | str]:
        try:
            return json.loads(self._cache_path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict[str, float | str]) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self._cache_path)

    # -- public --------------------------------------------------------------------------
    def cooldown_remaining(self) -> float:
        until = float(self._read().get("cooldown_until", 0.0))
        return max(0.0, until - self._now())

    def invalidate(self) -> None:
        data = self._read()
        data.pop("token", None)
        data.pop("fetched_at", None)
        self._write(data)

    def get_token(self) -> str:
        data = self._read()
        now = self._now()

        token = data.get("token")
        fetched_at = float(data.get("fetched_at", 0.0))
        if token and fetched_at >= _last_expiry_boundary(now):
            return str(token)

        remaining = self.cooldown_remaining()
        if remaining > 0:
            raise TokenCooldownError(
                f"Groww token fetch paused after a rate limit ({remaining:.0f}s left)."
            )

        try:
            new_token = self._fetcher(self._totp_token, self._totp_secret)
        except Exception as exc:
            if is_rate_limit_error(exc):
                data["cooldown_until"] = now + self._cooldown_sec
                self._write(data)
            raise
        data.pop("cooldown_until", None)
        data["token"] = new_token
        data["fetched_at"] = now
        self._write(data)
        return new_token
