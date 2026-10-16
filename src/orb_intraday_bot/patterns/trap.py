"""Bull trap / bear trap machine, started when an ORB fails (close back inside the OR).

Bull trap (failed ORH breakout, trade is SHORT; the bear trap is the exact mirror):

1. Sign candle: closes back below ORH. An aggressive follow-up entry (Case 1 or Case 2) is offered
   immediately at that close. The zone filter decides in Phase 5 whether it may be taken.
2. Reverse retest: the next green candle is the pullback candle. It does not have to touch ORH.
   The most recent green candle is used as the pullback if several come in a row.
3. Entry: price breaks the low of the pullback candle. Entry is that low (or the candle's open if
   it gapped through it).
4. A close above ORH again cancels the trap; the breakout machine then sees a fresh sign.

Both the aggressive entry and the confirmation entry carry the same `sign_id`. Only one trade is
taken per sign (enforced by the signal engine).

[Assumption] Target is the opposite OR level for both entries; the notes give the target only
for the confirmation entry.
"""

from __future__ import annotations

from orb_intraday_bot.core.models import Candle, Direction
from orb_intraday_bot.core.signals import (
    PatternEvent,
    PatternKind,
    Setup,
    SetupSignal,
    StepResult,
)


class TrapMachine:
    def __init__(
        self,
        *,
        level_name: str,
        level: float,
        trade_direction: Direction,
        case: int,
        sign_candle: Candle,
        target_ref: str,
    ) -> None:
        if case not in (1, 2):
            raise ValueError("case must be 1 or 2")
        self.level_name = level_name
        self.level = level
        self.direction = trade_direction
        self.case = case
        self.sign_candle = sign_candle
        self.target_ref = target_ref
        self.pullback: Candle | None = None
        self.done = False
        self._short = trade_direction is Direction.SHORT
        self.sign_id = (
            f"{sign_candle.start.date().isoformat()}|{level_name}|trap|{sign_candle.start:%H:%M}"
        )

    # -- first step: aggressive entry at the sign candle's close -------------------------
    def start(self) -> StepResult:
        out = StepResult()
        c = self.sign_candle
        setup = Setup.FOLLOWUP_CASE_1 if self.case == 1 else Setup.FOLLOWUP_CASE_2
        out.signals.append(
            SetupSignal(
                setup=setup,
                direction=self.direction,
                level_name=self.level_name,
                level_value=self.level,
                candle_start=c.start,
                entry_price=c.close,
                entry_basis="close of the candle that closed back inside",
                sign_id=self.sign_id,
                stop_level=None,
                stop_on="none",
                target_ref=self.target_ref,
                aggressive=True,
                case=self.case,
                note=f"Case {self.case}",
            )
        )
        return out

    def update(self, candle: Candle) -> StepResult:
        out = StepResult()
        if self.done:
            return out
        short = self._short
        beyond_again = candle.close > self.level if short else candle.close < self.level
        if beyond_again:
            self.done = True
            detail = f"close {candle.close} beyond again"
            out.events.append(self._event(PatternKind.TRAP_CANCELLED, candle, detail))
            return out

        pb = self.pullback
        if pb is not None:
            broke = candle.low < pb.low if short else candle.high > pb.high
            if broke:
                self.done = True
                trigger = pb.low if short else pb.high
                gapped = candle.open <= trigger if short else candle.open >= trigger
                price = candle.open if gapped else trigger
                basis = "pullback candle low broken" if short else "pullback candle high broken"
                if gapped:
                    basis += " (gap, filled at open)"
                out.events.append(self._event(PatternKind.TRAP_TRIGGERED, candle, basis))
                out.signals.append(
                    SetupSignal(
                        setup=Setup.TRAP_REVERSE_RETEST,
                        direction=self.direction,
                        level_name=self.level_name,
                        level_value=self.level,
                        candle_start=candle.start,
                        entry_price=price,
                        entry_basis=basis,
                        sign_id=self.sign_id,
                        stop_level=self.level,
                        stop_on="touch",
                        target_ref=self.target_ref,
                        case=self.case,
                        note=f"pullback candle {pb.start:%H:%M}",
                    )
                )
                return out

        pullback_candle = candle.close > candle.open if short else candle.close < candle.open
        if pullback_candle:
            self.pullback = candle
            out.events.append(self._event(PatternKind.TRAP_PULLBACK, candle, "pullback candle"))
        return out

    def _event(self, kind: PatternKind, candle: Candle, detail: str) -> PatternEvent:
        return PatternEvent(kind, self.level_name, self.direction, candle.start, detail)
