"""Consolidation at ORH / ORL, then a direct-entry breakout (notes 3.7).

"Multiple rejections in the OR (consolidation) for 45 mins to 1 hour, then a candle closes
above ORH / below ORL: enter directly at the breakout and do not wait for a retest."

Long side at ORH (the short side at ORL mirrors):

- A rejection is a candle that reaches the level (high within the tolerance of it, or above it)
  and still closes at or below it. Consecutive touching candles count as one rejection.
- Rejections older than `max_minutes` are forgotten. A close beyond the level clears them all.
- When a candle closes above ORH: if at least `min_touches` rejections are on record and the
  first of them is at least `min_minutes` old, enter at that close.

[Assumption] "Multiple" means at least `min_touches` (3). "45 to 60 minutes" is a rolling window:
the oldest rejection kept is at most 60 minutes old and must be at least 45 minutes old.

The entry shares its `sign_id` with the ORB machine's sign on the same candle, so only one of the
two is taken (the direct entry comes first, the retest-based ORB signal would come later).

Stop is the standard 35 points. Target is the OR range projected from the broken level, or the
next pivot, whichever is nearer (Phase 5 resolves the pivot; this signal carries the projection).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from orb_intraday_bot.core.models import Candle, Direction
from orb_intraday_bot.core.signals import (
    PatternEvent,
    PatternKind,
    Setup,
    SetupSignal,
    StepResult,
)


class ConsolidationMachine:
    def __init__(
        self,
        *,
        level_name: str,
        level: float,
        direction: Direction,
        min_touches: int,
        min_minutes: int,
        max_minutes: int,
        tolerance: float,
    ) -> None:
        self.level_name = level_name
        self.level = level
        self.direction = direction
        self.min_touches = min_touches
        self.min_minutes = min_minutes
        self.max_minutes = max_minutes
        self.tolerance = tolerance
        self.touches: list[datetime] = []
        self._in_touch = False
        self._long = direction is Direction.LONG

    def update(self, candle: Candle, orh: float | None, orl: float | None) -> StepResult:
        out = StepResult()
        long = self._long
        beyond = candle.close > self.level if long else candle.close < self.level

        cutoff = candle.start - timedelta(minutes=self.max_minutes)
        self.touches = [t for t in self.touches if t >= cutoff]

        if beyond:
            if self._qualifies(candle):
                out.signals.append(self._signal(candle, orh, orl))
            self.touches.clear()
            self._in_touch = False
            return out

        reached = (
            candle.high >= self.level - self.tolerance
            if long
            else candle.low <= self.level + self.tolerance
        )
        if reached:
            if not self._in_touch:
                self.touches.append(candle.start)
                out.events.append(
                    PatternEvent(
                        PatternKind.CONSOLIDATION_TOUCH,
                        self.level_name,
                        self.direction,
                        candle.start,
                        f"rejection #{len(self.touches)} at {self.level_name}",
                    )
                )
            self._in_touch = True
        else:
            self._in_touch = False
        return out

    def _qualifies(self, candle: Candle) -> bool:
        if len(self.touches) < self.min_touches:
            return False
        return candle.start - self.touches[0] >= timedelta(minutes=self.min_minutes)

    def _signal(self, candle: Candle, orh: float | None, orl: float | None) -> SetupSignal:
        projection: float | None = None
        if orh is not None and orl is not None:
            width = orh - orl
            projection = self.level + width if self._long else self.level - width
        return SetupSignal(
            setup=Setup.CONSOLIDATION_BREAKOUT,
            direction=self.direction,
            level_name=self.level_name,
            level_value=self.level,
            candle_start=candle.start,
            entry_price=candle.close,
            entry_basis="breakout candle close (direct entry, no retest)",
            # same format as BreakoutMachine.sign_id, so one trade per sign
            sign_id=f"{candle.start.date().isoformat()}|{self.level_name}|{candle.start:%H:%M}",
            stop_level=None,
            stop_on="none",
            target_ref="OR_PROJECTION_OR_PIVOT",
            target_price=projection,
            aggressive=True,        # a direct entry with no retest: blocked in the neutral zone
            note=f"{len(self.touches)} rejections over "
                 f"{int((candle.start - self.touches[0]).total_seconds() // 60)} min",
        )
