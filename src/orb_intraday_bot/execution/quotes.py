"""Option quotes with bid and ask: what a real order would meet.

The dry-run venue fills against these, and the executor uses them for the spread gate and the
entry price. Reading a quote never places anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from orb_intraday_bot.core.errors import DataUnavailableError


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    bid: float | None            # best bid, None when there is no bid
    ask: float | None            # best ask, None when there is no offer
    ltp: float | None
    time: datetime

    @property
    def spread_pct(self) -> float | None:
        """(ask - bid) / mid, or None when either side is missing."""
        if self.bid is None or self.ask is None or self.bid <= 0 or self.ask <= 0:
            return None
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid


class QuoteSource(Protocol):
    def quote(self, symbol: str) -> Quote:
        """The current quote. Raises `DataUnavailableError` if there is none."""
        ...


class NoQuoteError(DataUnavailableError, RuntimeError):
    pass


def round_up_to_tick(price: float, tick: float) -> float:
    """The smallest multiple of `tick` that is at least `price` (for a protective buy limit)."""
    steps = -(-round(price / tick, 6) // 1)
    return round(steps * tick, 4)


def round_down_to_tick(price: float, tick: float) -> float:
    """The largest multiple of `tick` that is at most `price` (for a protective sell limit)."""
    steps = round(price / tick, 6) // 1
    return round(steps * tick, 4)
