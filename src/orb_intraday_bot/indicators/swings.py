"""Swing highs and lows (fractals) with a configurable strength.

A swing high at index i needs `strength` candles on each side: its high is strictly above the
highs on the left and at least equal to the highs on the right. A swing is confirmed
`strength` candles after it formed, so detection is never look-ahead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from orb_intraday_bot.core.models import Candle


@dataclass(frozen=True, slots=True)
class Swing:
    kind: Literal["high", "low"]
    price: float
    index: int            # index in the day's candle list
    candle_start: datetime


class SwingTracker:
    def __init__(self, strength: int = 1) -> None:
        if strength < 1:
            raise ValueError("strength must be at least 1")
        self._k = strength
        self._day: date | None = None
        self._candles: list[Candle] = []
        self.swings: list[Swing] = []

    def _reset(self, day: date) -> None:
        self._day = day
        self._candles = []
        self.swings = []

    def update(self, candle: Candle) -> list[Swing]:
        """Add a candle and return swings confirmed by it."""
        day = candle.start.date()
        if day != self._day:
            self._reset(day)
        self._candles.append(candle)

        k = self._k
        i = len(self._candles) - 1 - k
        if i < k:
            return []
        c = self._candles[i]
        left = self._candles[i - k : i]
        right = self._candles[i + 1 : i + 1 + k]

        found: list[Swing] = []
        if all(c.high > x.high for x in left) and all(c.high >= x.high for x in right):
            found.append(Swing("high", c.high, i, c.start))
        if all(c.low < x.low for x in left) and all(c.low <= x.low for x in right):
            found.append(Swing("low", c.low, i, c.start))
        self.swings.extend(found)
        return found

    def last(self, kind: Literal["high", "low"], n: int = 1) -> list[Swing]:
        """The most recent `n` swings of a kind, oldest first."""
        return [s for s in self.swings if s.kind == kind][-n:]
