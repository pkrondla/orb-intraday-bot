"""Read-only Groww client.

This is the ONLY object in the bot that talks to Groww. It whitelists read methods; every other
attribute (place_order, modify_order, cancel_order, smart orders, positions, holdings, margins...)
is simply not reachable. There is no order path in this project.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from orb_intraday_bot.adapters.groww.token import TokenProvider

# Methods the bot may call. Keep this list minimal.
ALLOWED_METHODS: frozenset[str] = frozenset(
    {
        "get_historical_candles",
        "get_expiries",
        "get_contracts",
        "get_option_chain",
        "get_greeks",
        "get_ltp",
        "get_quote",
        "get_ohlc",
        "get_all_instruments",
        "get_instrument_by_exchange_and_trading_symbol",
        "get_instrument_by_groww_symbol",
        "get_user_profile",
    }
)

# Never reachable. Listed so tests and the startup guard can assert they stay unreachable.
BLOCKED_METHODS: frozenset[str] = frozenset(
    {
        "place_order",
        "modify_order",
        "cancel_order",
        "create_smart_order",
        "modify_smart_order",
        "cancel_smart_order",
    }
)

# Pure constants (exchange, segment, candle interval...) are safe to read.
_CONSTANT_PREFIXES = ("CANDLE_INTERVAL_", "EXCHANGE_", "SEGMENT_")

_TRANSIENT = (
    "429", "rate limit", "ratelimit", "503", "502", "500", "server error",
    "timeout", "timed out", "connection", "temporarily unavailable",
)
_AUTH = ("401", "403", "access forbidden", "unauthor", "invalid token", "token expired")

_MIN_CALL_INTERVAL_SEC = 0.12   # about 8 calls/sec, under the 10/sec live-data limit
_MAX_CONCURRENT = 3


class ForbiddenBrokerCall(AttributeError):
    """Raised for any Groww attribute outside the read-only whitelist."""


class ReadOnlyGrowwClient:
    def __init__(
        self,
        token_manager: TokenProvider,
        api_factory: Callable[[str], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._tokens = token_manager
        self._api_factory = api_factory or self._default_factory
        self._sleep = sleep
        self._api: Any = None
        self._lock = threading.Lock()
        self._sem = threading.Semaphore(_MAX_CONCURRENT)
        self._last_call = 0.0

    @staticmethod
    def _default_factory(token: str) -> Any:
        from growwapi import GrowwAPI

        return GrowwAPI(token)

    def _ensure_api(self) -> Any:
        with self._lock:
            if self._api is None:
                self._api = self._api_factory(self._tokens.get_token())
            return self._api

    def _reset_api(self) -> None:
        with self._lock:
            self._api = None
        self._tokens.invalidate()

    def _throttle(self) -> None:
        with self._lock:
            wait = self._last_call + _MIN_CALL_INTERVAL_SEC - time.monotonic()
            if wait > 0:
                self._sleep(wait)
            self._last_call = time.monotonic()

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        reauthed = False
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                api = self._ensure_api()
                with self._sem:
                    self._throttle()
                    return getattr(api, name)(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                msg = str(exc).lower()
                if not reauthed and any(m in msg for m in _AUTH):
                    reauthed = True
                    self._reset_api()
                    continue
                if attempt < 3 and any(m in msg for m in _TRANSIENT):
                    self._sleep(2**attempt)   # 2s, 4s
                    continue
                break
        assert last_exc is not None
        raise last_exc

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in ALLOWED_METHODS:
            def method(*args: Any, **kwargs: Any) -> Any:
                return self._call(name, *args, **kwargs)

            method.__name__ = name
            return method
        if name.startswith(_CONSTANT_PREFIXES):
            return getattr(self._get_class(), name)
        raise ForbiddenBrokerCall(
            f"'{name}' is not available: this bot is read-only and never places orders."
        )

    @staticmethod
    def _get_class() -> Any:
        from growwapi import GrowwAPI

        return GrowwAPI

    def __dir__(self) -> list[str]:
        return sorted(ALLOWED_METHODS)
