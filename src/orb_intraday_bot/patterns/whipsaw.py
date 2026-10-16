"""Additional whipsaw: continuation re-entry after repeated rejections of a level (notes 3.6).

Started after a parent trade confirms: an ORB (ORH/ORL) or the reverse-retest entry of a trap.

Long side (the parent is long; shorts mirror):

- A rejection is a candle that comes back to the level (low within the retest tolerance of it)
  without breaking the stop. Consecutive touching candles count as one rejection.
- The stop must not be hit. For an ORB parent that means no close back below the level; for a trap
  parent it means price never trades through the level. Otherwise tracking stops.
- After `min_rejections` rejections, price breaking above the last rejection candle's high is the
  continuation entry (a gap through it fills at the open). One entry per parent.

[Assumption] Detection is exactly as above; the notes only say "two or more rejections at the
level, SL not hit, momentum comes after the whipsaws" and show it on charts.
"""

from __future__ import annotations

from datetime import datetime

from orb_intraday_bot.core.models import Candle, Direction
from orb_intraday_bot.core.signals import (
    PatternEvent,
    PatternKind,
    Setup,
    SetupSignal,
    StepResult,
    StopOn,
)


class RejectionWhipsawMachine:
    def __init__(
        self,
        *,
        level_name: str,
        level: float,
        direction: Direction,
        parent: str,                 # "ORB" or "TRAP"
        stop_on: StopOn,
        target_ref: str,
        min_rejections: int,
        tolerance: float,
        parent_candle: datetime,
    ) -> None:
        self.level_name = level_name
        self.level = level
        self.direction = direction
        self.parent = parent
        self.stop_on = stop_on
        self.target_ref = target_ref
        self.min_rejections = min_rejections
        self.tolerance = tolerance
        self.rejections = 0
        self.last_rejection: Candle | None = None
        self.done = False
        self._in_touch = False
        self._long = direction is Direction.LONG
        self.sign_id = (
            f"{parent_candle.date().isoformat()}|{level_name}|whipsaw|{parent.lower()}|"
            f"{parent_candle:%H:%M}"
        )

    def update(self, candle: Candle) -> StepResult:
        out = StepResult()
        if self.done:
            return out
        long = self._long

        if self.stop_on == "close":
            stopped = candle.close < self.level if long else candle.close > self.level
        else:
            stopped = candle.low < self.level if long else candle.high > self.level
        if stopped:
            self.done = True
            out.events.append(self._event(PatternKind.WHIPSAW_CANCELLED, candle, "stop condition"))
            return out

        last = self.last_rejection
        if last is not None and self.rejections >= self.min_rejections:
            trigger = last.high if long else last.low
            broke = candle.high > trigger if long else candle.low < trigger
            if broke:
                self.done = True
                gapped = candle.open >= trigger if long else candle.open <= trigger
                price = candle.open if gapped else trigger
                basis = f"break of last rejection candle {'high' if long else 'low'}"
                if gapped:
                    basis += " (gap, filled at open)"
                out.signals.append(
                    SetupSignal(
                        setup=Setup.ADDITIONAL_WHIPSAW,
                        direction=self.direction,
                        level_name=self.level_name,
                        level_value=self.level,
                        candle_start=candle.start,
                        entry_price=price,
                        entry_basis=basis,
                        sign_id=self.sign_id,
                        stop_level=self.level,
                        stop_on=self.stop_on,
                        target_ref=self.target_ref,
                        note=f"{self.rejections} rejections after {self.parent}",
                    )
                )
                return out

        touched = (
            candle.low <= self.level + self.tolerance
            if long
            else candle.high >= self.level - self.tolerance
        )
        if touched:
            if not self._in_touch:
                self.rejections += 1
                out.events.append(
                    self._event(
                        PatternKind.WHIPSAW_REJECTION, candle, f"rejection #{self.rejections}"
                    )
                )
            self._in_touch = True
            self.last_rejection = candle
        else:
            self._in_touch = False
        return out

    def _event(self, kind: PatternKind, candle: Candle, detail: str) -> PatternEvent:
        return PatternEvent(kind, self.level_name, self.direction, candle.start, detail)
