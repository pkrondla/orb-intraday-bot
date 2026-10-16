"""Capability-scoped Groww client that may place orders.

This is the ONLY module outside tests that may call place_order / cancel_order /
modify_order / create_smart_order. It is built only with a separate trading TOTP key.
The read-only client stays read-only and is used for all market data.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from orb_intraday_bot.adapters.groww.token import TokenProvider

# Methods this client may call. Keep the list explicit.
ALLOWED_METHODS: frozenset[str] = frozenset(
    {
        "place_order",
        "modify_order",
        "cancel_order",
        "create_smart_order",
        "modify_smart_order",
        "cancel_smart_order",
        "get_order_status",
        "get_order_status_by_reference",
        "get_order_detail",
        "get_order_list",
        "get_trade_list_for_order",
        "get_smart_order",
        "get_smart_order_list",
        "get_available_margin_details",
        "get_order_margin_details",
        "get_position_for_trading_symbol",
        "get_positions_for_user",
        "get_ltp",
        "get_quote",
    }
)

ORDER_METHODS: frozenset[str] = frozenset(
    {
        "place_order",
        "modify_order",
        "cancel_order",
        "create_smart_order",
        "modify_smart_order",
        "cancel_smart_order",
    }
)

_CONSTANT_PREFIXES = (
    "ORDER_TYPE_", "PRODUCT_", "SEGMENT_", "EXCHANGE_", "TRANSACTION_TYPE_",
    "SMART_ORDER_", "VALIDITY_", "TRIGGER_DIRECTION_",
)

_TRANSIENT = (
    "429", "rate limit", "ratelimit", "503", "502", "500", "server error",
    "timeout", "timed out", "connection", "temporarily unavailable",
)
_AUTH = ("401", "403", "access forbidden", "unauthor", "invalid token", "token expired")

_MIN_CALL_INTERVAL_SEC = 0.2
_MAX_CONCURRENT = 2


class ForbiddenTradingCall(AttributeError):
    """Raised for any Groww attribute outside the trading whitelist."""


class GrowwTradingClient:
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
    def _default_factory(token: str) -> Any:  # pragma: no cover - real SDK
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
                    self._sleep(2**attempt)
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
        raise ForbiddenTradingCall(
            f"'{name}' is not available on GrowwTradingClient."
        )

    @staticmethod
    def _get_class() -> Any:  # pragma: no cover - real SDK constants
        from growwapi import GrowwAPI

        return GrowwAPI

    def __dir__(self) -> list[str]:
        return sorted(ALLOWED_METHODS)
