from __future__ import annotations

from typing import Any

import pytest

from orb_intraday_bot.adapters.groww import ForbiddenBrokerCall
from orb_intraday_bot.adapters.groww.feed import (
    ALLOWED_FEED_METHODS,
    BLOCKED_FEED_METHODS,
    ReadOnlyGrowwFeed,
)


class FakeTokens:
    def get_token(self) -> str:
        return "tok"

    def invalidate(self) -> None:
        pass


class FakeFeed:
    def subscribe_ltp(self, instruments: list[dict[str, Any]], on_data_received: Any = None) -> str:
        return "subscribed"

    def subscribe_fno_order_updates(self, *a: Any, **k: Any) -> None:
        raise AssertionError("order updates must never be subscribed")

    def get_fno_position_update(self, *a: Any, **k: Any) -> None:
        raise AssertionError("position updates must never be read")


def make() -> ReadOnlyGrowwFeed:
    return ReadOnlyGrowwFeed(FakeTokens(), feed_factory=lambda t: FakeFeed())


def test_ltp_subscription_allowed() -> None:
    assert make().subscribe_ltp([{"exchange": "NSE"}]) == "subscribed"


@pytest.mark.parametrize("name", sorted(BLOCKED_FEED_METHODS))
def test_order_and_position_feeds_unreachable(name: str) -> None:
    feed = make()
    assert not hasattr(feed, name)
    with pytest.raises(ForbiddenBrokerCall):
        getattr(feed, name)


def test_allow_and_block_lists_disjoint() -> None:
    assert not (ALLOWED_FEED_METHODS & BLOCKED_FEED_METHODS)
