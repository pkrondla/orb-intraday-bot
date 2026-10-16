"""Consolidation + fake breakdown -> sharp move up (long), and its mirror (notes 3.8).

"Consolidation, fake breakdown and sharp move up. Entry is near the 50% retracement of the sharp
reversal leg. The stop is beyond the fake extreme, or 35 points. The target is the opposite end
of the consolidation." The fake breakout followed by a sharp move down is the short mirror.

Long side (fake breakdown). Everything below is done in a "view" where the machine always looks
for a long; for the short side prices are negated.

1. Range: the last `consolidation_candles` candles are a consolidation if their total range is at
   most `max_range_points` and each edge has been touched at least `edge_touches` times (a touch is
   within 15% of the range width of the edge; consecutive candles count once).
2. Fake break: a candle trades below the range low. If a candle closes back above the range low
   within `max_fake_candles` candles the break was fake; otherwise it was real and the setup is off.
3. Reversal leg: from the fake extreme (lowest low since the break) up to the highest high since.
   Once the leg is at least `min_reversal_leg_points` ("sharp"), the entry is the 50% retracement
   (`entries.fake_breakout_entry_retracement`) of the leg. A later candle whose low reaches it fills
   there.
4. Cancelled if price trades below the fake extreme (stop), closes below the range low again,
   reaches the opposite end of the range without a retracement, or `max_wait_candles` pass.

Stop is the fake extreme (or 35 points, whichever is closer; resolved in Phase 5). Target is the
opposite end of the consolidation.

[Assumption] Range definition, edge touches, "sharp" threshold and the leg reference (fake extreme
to the reversal's extreme) are not in the notes; all are config values under `fake_break`.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from enum import StrEnum

from orb_intraday_bot.core.models import Candle, Direction
from orb_intraday_bot.core.signals import (
    PatternEvent,
    PatternKind,
    Setup,
    SetupSignal,
    StepResult,
)


class _Phase(StrEnum):
    IDLE = "IDLE"
    BROKE = "BROKE"
    REVERSING = "REVERSING"


class FakeBreakMachine:
    def __init__(
        self,
        *,
        direction: Direction,
        consolidation_candles: int,
        max_range_points: float,
        edge_touches: int,
        max_fake_candles: int,
        min_reversal_leg_points: float,
        max_wait_candles: int,
        retracement: float,
    ) -> None:
        self.direction = direction
        self.k = consolidation_candles
        self.max_range = max_range_points
        self.edge_touches = edge_touches
        self.max_fake = max_fake_candles
        self.min_leg = min_reversal_leg_points
        self.max_wait = max_wait_candles
        self.retracement = retracement
        self._long = direction is Direction.LONG
        self._window: deque[tuple[float, float]] = deque(maxlen=consolidation_candles)  # (h, l)
        self.phase = _Phase.IDLE
        self._rh = 0.0
        self._rl = 0.0
        self._fake = 0.0
        self._since = 0
        self._wait = 0
        self._rev_high = 0.0
        self._break_start: datetime | None = None

    # -- view helpers -------------------------------------------------------------------
    def _view(self, c: Candle) -> tuple[float, float, float]:
        """(high, low, close) in the long view."""
        if self._long:
            return c.high, c.low, c.close
        return -c.low, -c.high, -c.close

    def _real(self, price: float) -> float:
        return price if self._long else -price

    def _event(self, kind: PatternKind, candle: Candle, detail: str) -> PatternEvent:
        name = "RANGE_LOW" if self._long else "RANGE_HIGH"
        return PatternEvent(kind, name, self.direction, candle.start, detail)

    def _edges_ok(self, rh: float, rl: float) -> bool:
        tol = 0.15 * (rh - rl)
        top = low = 0
        top_prev = low_prev = False
        for h, l_ in self._window:
            at_top = h >= rh - tol
            at_low = l_ <= rl + tol
            top += int(at_top and not top_prev)
            low += int(at_low and not low_prev)
            top_prev, low_prev = at_top, at_low
        return top >= self.edge_touches and low >= self.edge_touches

    def _cancel(self, out: StepResult, candle: Candle, why: str) -> None:
        out.events.append(self._event(PatternKind.FAKE_CANCELLED, candle, why))
        self.phase = _Phase.IDLE

    # -- main ------------------------------------------------------------------------------
    def update(self, candle: Candle) -> StepResult:
        out = StepResult()
        high, low, close = self._view(candle)

        if self.phase is _Phase.IDLE:
            if len(self._window) == self.k:
                rh = max(h for h, _ in self._window)
                rl = min(lo for _, lo in self._window)
                if rh - rl <= self.max_range and self._edges_ok(rh, rl) and low < rl:
                    self._rh, self._rl, self._fake = rh, rl, low
                    self._since = 1
                    self._break_start = candle.start
                    self.phase = _Phase.BROKE
                    out.events.append(
                        self._event(
                            PatternKind.FAKE_BREAK,
                            candle,
                            f"left range {self._real(rl)}..{self._real(rh)}",
                        )
                    )
                    if close > rl:
                        self._start_reversal(high, out, candle)
        elif self.phase is _Phase.BROKE:
            self._fake = min(self._fake, low)
            self._since += 1
            if close > self._rl:
                self._start_reversal(high, out, candle)
            elif self._since > self.max_fake:
                self._cancel(out, candle, "no close back inside: a real break")
        else:
            self._reversing(candle, high, low, close, out)

        self._window.append((high, low))
        return out

    def _start_reversal(self, high: float, out: StepResult, candle: Candle) -> None:
        self.phase = _Phase.REVERSING
        self._rev_high = high
        self._wait = 0
        out.events.append(
            self._event(
                PatternKind.FAKE_REVERSAL, candle, f"fake extreme {self._real(self._fake)}"
            )
        )

    def _reversing(
        self, candle: Candle, high: float, low: float, close: float, out: StepResult
    ) -> None:
        if low < self._fake:
            self._cancel(out, candle, "traded below the fake extreme")
            return
        if close < self._rl:
            self._cancel(out, candle, "closed below the range again")
            return
        leg = self._rev_high - self._fake
        if leg >= self.min_leg:
            entry = self._rev_high - self.retracement * leg
            if entry >= self._rh:
                self._cancel(out, candle, "retracement entry is above the target")
                return
            if low <= entry:
                out.signals.append(self._signal(candle, entry))
                self.phase = _Phase.IDLE
                return
        self._rev_high = max(self._rev_high, high)
        self._wait += 1
        if self._rev_high >= self._rh:
            self._cancel(out, candle, "reached the range end without a retracement")
        elif self._wait > self.max_wait:
            self._cancel(out, candle, "no retracement within the wait limit")

    def _signal(self, candle: Candle, entry_view: float) -> SetupSignal:
        assert self._break_start is not None
        name = "RANGE_LOW" if self._long else "RANGE_HIGH"
        leg = self._rev_high - self._fake
        return SetupSignal(
            setup=Setup.FAKE_BREAK,
            direction=self.direction,
            level_name=name,
            level_value=self._real(self._rl),
            candle_start=candle.start,
            entry_price=self._real(entry_view),
            entry_basis=f"{self.retracement:.0%} retracement of the reversal leg",
            sign_id=(
                f"{candle.start.date().isoformat()}|FAKE|{self.direction.value}|"
                f"{self._break_start:%H:%M}"
            ),
            stop_level=self._real(self._fake),
            stop_on="touch",
            target_ref="RANGE_OPPOSITE_END",
            target_price=self._real(self._rh),
            note=f"fake extreme {self._real(self._fake)}, leg {leg:.1f} pts",
        )
