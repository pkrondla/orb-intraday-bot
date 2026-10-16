"""Standard ORB state machine (sign -> retest -> confirmation), mirrored for shorts.

One instance tracks one level in one direction: ORH/R2 for longs, ORL/S2 for shorts. R2 and S2
are traded with exactly the same machine ("R2 acting as ORH").

Candle-close rules (all evaluated on closed candles, long side shown):

- IDLE -> SIGN: a candle closes above the level.
- SIGN: a candle whose low comes within `retest_tolerance` of the level (or below it) while it does
  not close back below the level is the retest. The candles from the sign candle up to the retest
  candle are kept.
- At the retest the confirmation level (the "previous swing") is fixed: the most recent swing high
  before the retest pullback. A swing high is a candle whose high is not below the one before it
  and is strictly above the one after it, among the sign candle .. retest candle. The sign candle
  counts as a start of the leg. If no swing high exists yet (price has not pulled back in highs,
  e.g. the retest candle itself is the highest), the highest high from the sign candle through the
  retest candle is used.
- RETESTED: a later candle whose high exceeds the swing high confirms the ORB. A break is enough;
  a close is not required. There is one retest per sign.
- A candle closing back below the level at any point is a failure. Before confirmation it is a
  Case 1 failure, afterwards Case 2. The machine then returns to IDLE, and a fresh close above
  the level starts a new sign. Failure uses the plain close, with no tolerance.

Source: notes say "retests ORH and makes high to previous swing/candle". "Most recent swing high
before the pullback", the tolerance, and the fallback are assumptions (see intraday-setup.md,
open items).
[Assumption] Retest and confirmation are never the same candle: the order of the dip and the
spike inside one candle is unknown, so the bot does not assume the favourable order.
[Assumption] If a candle both breaks the swing and closes back inside, the close wins (failure).

Entry: the previous candle's low (long) or high (short). If the confirmation candle traded at
that price the fill is there, otherwise it fills at the confirmation candle's close.
"""

from __future__ import annotations

from enum import StrEnum

from orb_intraday_bot.core.models import Candle, Direction
from orb_intraday_bot.core.signals import (
    PatternEvent,
    PatternKind,
    Setup,
    SetupSignal,
    StepResult,
)


class BreakoutPhase(StrEnum):
    IDLE = "IDLE"
    SIGN = "SIGN"
    RETESTED = "RETESTED"
    CONFIRMED = "CONFIRMED"


class BreakoutMachine:
    def __init__(
        self,
        *,
        level_name: str,
        level: float,
        direction: Direction,
        setup: Setup,
        target_ref: str,
        retest_tolerance: float = 0.0,
    ) -> None:
        self.level_name = level_name
        self.level = level
        self.direction = direction
        self.setup = setup
        self.target_ref = target_ref
        self.retest_tolerance = retest_tolerance
        self.phase = BreakoutPhase.IDLE
        self.sign_candle: Candle | None = None
        self.swing: float | None = None
        self._leg: list[Candle] = []
        self._prev: Candle | None = None
        self._long = direction is Direction.LONG

    @property
    def sign_id(self) -> str | None:
        if self.sign_candle is None:
            return None
        return f"{self.sign_candle.start.date().isoformat()}|{self.level_name}|" \
               f"{self.sign_candle.start:%H:%M}"

    def update(self, candle: Candle) -> StepResult:
        out = StepResult()
        prev = self._prev
        self._prev = candle
        lvl = self.level
        long = self._long

        beyond = candle.close > lvl if long else candle.close < lvl
        failed = candle.close < lvl if long else candle.close > lvl
        if long:
            touched = candle.low <= lvl + self.retest_tolerance
        else:
            touched = candle.high >= lvl - self.retest_tolerance

        if self.phase is BreakoutPhase.IDLE:
            if beyond:
                self.phase = BreakoutPhase.SIGN
                self.sign_candle = candle
                self._leg = [candle]
                out.events.append(self._event(PatternKind.SIGN, candle, f"close {candle.close}"))
            return out

        if failed:
            kind = (
                PatternKind.FAILED_CASE_2
                if self.phase is BreakoutPhase.CONFIRMED
                else PatternKind.FAILED_CASE_1
            )
            out.events.append(self._event(kind, candle, f"close {candle.close} back inside"))
            self.phase = BreakoutPhase.IDLE
            self._leg = []
            return out

        if self.phase is BreakoutPhase.SIGN:
            self._leg.append(candle)
            if touched:
                self.phase = BreakoutPhase.RETESTED
                self.swing = self._swing_level()
                out.events.append(
                    self._event(
                        PatternKind.RETEST,
                        candle,
                        f"{'low' if long else 'high'} {candle.low if long else candle.high} "
                        f"vs level {self.level}, swing {self.swing}",
                    )
                )
        elif self.phase is BreakoutPhase.RETESTED:
            assert self.swing is not None
            broke = candle.high > self.swing if long else candle.low < self.swing
            if broke and prev is not None:
                self.phase = BreakoutPhase.CONFIRMED
                out.events.append(
                    self._event(PatternKind.CONFIRMED, candle, f"broke swing {self.swing}")
                )
                out.signals.append(self._signal(candle, prev))
        return out

    # -- helpers -------------------------------------------------------------------------
    def _swing_level(self) -> float:
        """Most recent swing high (low) before the retest, else the leg's extreme."""
        leg = self._leg
        # value to compare: high for longs, negated low for shorts (so "higher" means better)
        vals = [c.high if self._long else -c.low for c in leg]
        for i in range(len(leg) - 2, -1, -1):          # exclude the retest candle itself
            starts_leg_or_not_lower = i == 0 or vals[i] >= vals[i - 1]
            if starts_leg_or_not_lower and vals[i] > vals[i + 1]:
                return vals[i] if self._long else -vals[i]
        best = max(vals)
        return best if self._long else -best

    def _event(self, kind: PatternKind, candle: Candle, detail: str) -> PatternEvent:
        return PatternEvent(kind, self.level_name, self.direction, candle.start, detail)

    def _signal(self, candle: Candle, prev: Candle) -> SetupSignal:
        if self._long:
            ref = prev.low
            touched = candle.low <= ref
            what = "previous candle low"
        else:
            ref = prev.high
            touched = candle.high >= ref
            what = "previous candle high"
        if touched:
            price, basis = ref, f"{what} (touched)"
        else:
            price, basis = candle.close, "confirmation candle close (reference not traded)"
        assert self.sign_id is not None
        return SetupSignal(
            setup=self.setup,
            direction=self.direction,
            level_name=self.level_name,
            level_value=self.level,
            candle_start=candle.start,
            entry_price=price,
            entry_basis=basis,
            sign_id=self.sign_id,
            stop_level=self.level,
            stop_on="close",
            target_ref=self.target_ref,
            note=f"swing {self.swing} broken; reference {ref}",
        )
