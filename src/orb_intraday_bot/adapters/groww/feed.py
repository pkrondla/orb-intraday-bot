"""Read-only wrapper over GrowwFeed (live ticks).

GrowwFeed also exposes order-update and position-update subscriptions. Those are not reachable
here: only LTP subscription and consumption are whitelisted.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from orb_intraday_bot.adapters.groww.client import ForbiddenBrokerCall
from orb_intraday_bot.adapters.groww.token import TokenProvider

ALLOWED_FEED_METHODS: frozenset[str] = frozenset(
    {"subscribe_ltp", "unsubscribe_ltp", "get_ltp", "consume"}
)
BLOCKED_FEED_METHODS: frozenset[str] = frozenset(
    {
        "subscribe_fno_order_updates",
        "get_fno_order_update",
        "subscribe_fno_position_updates",
        "get_fno_position_update",
        "subscribe_equity_order_updates",
        "get_equity_order_update",
    }
)


class ReadOnlyGrowwFeed:
    def __init__(
        self,
        token_provider: TokenProvider,
        feed_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self._feed_factory = feed_factory or self._default_factory
        self._feed = self._feed_factory(token_provider.get_token())

    @staticmethod
    def _default_factory(token: str) -> Any:
        from growwapi import GrowwAPI, GrowwFeed

        return GrowwFeed(GrowwAPI(token))

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in ALLOWED_FEED_METHODS:
            return getattr(self._feed, name)
        raise ForbiddenBrokerCall(
            f"'{name}' is not available: the feed is read-only (LTP only)."
        )

    def __dir__(self) -> list[str]:
        return sorted(ALLOWED_FEED_METHODS)
