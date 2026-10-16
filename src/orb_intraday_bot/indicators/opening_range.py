"""Opening range (ORH / ORL) from runs of higher/lower highs and lows.

Spec (intraday-setup.md section 2):
- ORH: from the day's first candle, follow candles that each make a higher OR EQUAL high than the
  one before. The first candle with a lower high ends the run, and ORH = the previous candle's
  high (the run's peak).
- ORL: the mirror, with lower or equal lows.
- No fixed length. The two runs are independent. Each level is set once per day, at the close of
  the candle that ends its run, and reset on a new day.
"""

from __future__ import annotations

from datetime import date

from orb_intraday_bot.core.events import LevelSet
from orb_intraday_bot.core.models import Candle


class OpeningRangeTracker:
    def __init__(self) -> None:
        self._day: date | None = None
        self._prev: Candle | None = None
        self.orh: float | None = None
        self.orl: float | None = None

    @property
    def is_complete(self) -> bool:
        return self.orh is not None and self.orl is not None

    def _reset(self, day: date) -> None:
        self._day = day
        self._prev = None
        self.orh = None
        self.orl = None

    def update(self, candle: Candle) -> list[LevelSet]:
        day = candle.start.date()
        if day != self._day:
            self._reset(day)

        events: list[LevelSet] = []
        prev = self._prev
        if prev is not None:
            if self.orh is None and candle.high < prev.high:
                self.orh = prev.high
                events.append(LevelSet("ORH", prev.high, candle.start))
            if self.orl is None and candle.low > prev.low:
                self.orl = prev.low
                events.append(LevelSet("ORL", prev.low, candle.start))
        self._prev = candle
        return events
