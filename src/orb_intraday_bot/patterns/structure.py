"""2LL / 3LL (inside a rise) and 2HH / 3HH (inside a fall) continuation whipsaws (notes 3.8).

Rule of LL and HH: every rise has at least one lower low, every fall at least one higher high.
A pullback inside a rise that makes two or three lower lows is a whipsaw. Entry is long on the
break above the pullback's high (continuation). The fall mirrors it with higher highs.

All maths is done in a "view" where the machine always looks at a rise: for a fall the prices are
negated, so a higher high becomes a lower low and so on.

State (rise view):

- `run_high`: highest high of the current rise, `peak_low`: low of the candle that made it.
- `rise_low`: low the rise started from; stepped up to the pullback low each time a pullback holds.
- `pb_low` / `n`: lowest low of the current pullback and how many candles made a new low in it
  (the first reference is the peak candle's low).
- A new high ends the pullback. If `n` is 2 or 3 and the rise is at least `min_rise_points` tall,
  that break is the signal; entry is the old high (a gap through it fills at the open).
- If the pullback undercuts `rise_low` or makes more than `max_count` lower lows, the structure is
  broken and the machine starts over from the current candle.

[Assumption] "Rise" and "pullback" are defined as above, with `min_rise_points` (20) to ignore
noise. Stop is the pullback extreme or 35 points, whichever is closer. Target is R2 (long) or S2
(short), as for the other trend setups. The notes show this on charts only.
"""

from __future__ import annotations

from orb_intraday_bot.core.models import Candle, Direction
from orb_intraday_bot.core.signals import Setup, SetupSignal, StepResult


class StructureWhipsawMachine:
    def __init__(
        self,
        *,
        direction: Direction,
        min_count: int = 2,
        max_count: int = 3,
        min_rise_points: float = 20.0,
    ) -> None:
        self.direction = direction
        self.min_count = min_count
        self.max_count = max_count
        self.min_rise_points = min_rise_points
        self._long = direction is Direction.LONG
        self._run_high: float | None = None
        self._peak_low = 0.0
        self._rise_low = 0.0
        self._pb_low: float | None = None
        self._n = 0

    # -- internals -----------------------------------------------------------------------
    def _view(self, c: Candle) -> tuple[float, float, float]:
        """(open, high, low) in the rise view."""
        if self._long:
            return c.open, c.high, c.low
        return -c.open, -c.low, -c.high

    def _real(self, price: float) -> float:
        return price if self._long else -price

    def _fresh(self, high: float, low: float) -> None:
        self._run_high = high
        self._peak_low = low
        self._rise_low = low
        self._pb_low = None
        self._n = 0

    # -- public ----------------------------------------------------------------------------
    @property
    def lower_lows(self) -> int:
        """Lower lows (or higher highs) counted in the current pullback."""
        return self._n

    def update(self, candle: Candle) -> StepResult:
        out = StepResult()
        o, high, low = self._view(candle)
        if self._run_high is None:
            self._fresh(high, low)
            return out

        if high > self._run_high:                       # the pullback (if any) is over
            if self._pb_low is not None:
                tall_enough = self._run_high - self._rise_low >= self.min_rise_points
                if self.min_count <= self._n <= self.max_count and tall_enough:
                    out.signals.append(self._signal(candle, o))
                self._rise_low = self._pb_low           # the pullback held: new higher low
            self._run_high = high
            self._peak_low = low
            self._pb_low = None
            self._n = 0
            return out

        ref = self._pb_low if self._pb_low is not None else self._peak_low
        if low < ref:
            self._n += 1
            self._pb_low = low
        else:
            self._pb_low = ref
        if self._pb_low <= self._rise_low or self._n > self.max_count:
            self._fresh(high, low)
        return out

    def _signal(self, candle: Candle, open_view: float) -> SetupSignal:
        assert self._run_high is not None and self._pb_low is not None
        gapped = open_view >= self._run_high
        entry_view = open_view if gapped else self._run_high
        n = self._n
        name = f"{n}{'LL' if self._long else 'HH'}"
        basis = "break of the pullback " + ("high" if self._long else "low")
        if gapped:
            basis += " (gap, filled at open)"
        return SetupSignal(
            setup=Setup.STRUCTURE_WHIPSAW,
            direction=self.direction,
            level_name=name,
            level_value=self._real(self._pb_low),
            candle_start=candle.start,
            entry_price=self._real(entry_view),
            entry_basis=basis,
            sign_id=f"{candle.start.date().isoformat()}|{name}|{candle.start:%H:%M}",
            stop_level=self._real(self._pb_low),
            stop_on="touch",
            target_ref="R2" if self._long else "S2",
            note=f"{name} pullback, rise {self._run_high - self._rise_low:.1f} pts",
        )
