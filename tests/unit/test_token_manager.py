from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from orb_intraday_bot.adapters.groww import TokenCooldownError, TokenManager
from orb_intraday_bot.core.clock import IST


def ts(day: int, hh: int, mm: int = 0) -> float:
    return datetime(2026, 10, day, hh, mm, tzinfo=IST).timestamp()


class Fetcher:
    def __init__(self, errors: list[Exception] | None = None) -> None:
        self.count = 0
        self.errors = errors or []

    def __call__(self, key: str, secret: str) -> str:
        self.count += 1
        if self.errors:
            raise self.errors.pop(0)
        return f"token-{self.count}"


def test_token_is_cached_within_a_day(tmp_path: Path) -> None:
    fetch = Fetcher()
    now = [ts(15, 9)]
    mgr = TokenManager("k", "s", tmp_path / "t.json", fetcher=fetch, now=lambda: now[0])
    assert mgr.get_token() == "token-1"
    now[0] = ts(15, 14)
    assert mgr.get_token() == "token-1"
    assert fetch.count == 1


def test_new_token_after_daily_expiry(tmp_path: Path) -> None:
    fetch = Fetcher()
    now = [ts(15, 9)]
    mgr = TokenManager("k", "s", tmp_path / "t.json", fetcher=fetch, now=lambda: now[0])
    mgr.get_token()
    now[0] = ts(16, 7)   # after 06:00 next day
    assert mgr.get_token() == "token-2"


def test_cache_survives_restart(tmp_path: Path) -> None:
    fetch = Fetcher()
    path = tmp_path / "t.json"
    TokenManager("k", "s", path, fetcher=fetch, now=lambda: ts(15, 9)).get_token()
    TokenManager("k", "s", path, fetcher=fetch, now=lambda: ts(15, 10)).get_token()
    assert fetch.count == 1


def test_rate_limit_cooldown_is_persisted(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    fetch = Fetcher(errors=[RuntimeError("429 Too Many Requests")])
    now = [ts(15, 9)]
    mgr = TokenManager("k", "s", path, cooldown_sec=180, fetcher=fetch, now=lambda: now[0])
    with pytest.raises(RuntimeError):
        mgr.get_token()
    # A "restarted" process must not call the endpoint during the cooldown.
    restarted = TokenManager("k", "s", path, cooldown_sec=180, fetcher=fetch, now=lambda: now[0])
    with pytest.raises(TokenCooldownError):
        restarted.get_token()
    assert fetch.count == 1
    now[0] += 181
    assert restarted.get_token() == "token-2"


def test_missing_credentials_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        TokenManager("", "", tmp_path / "t.json")
