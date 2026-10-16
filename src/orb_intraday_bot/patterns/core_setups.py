"""Phase 3 detector: ORB longs/shorts, R2/S2 breakouts, traps and Case 1/2 follow-up failures.

One detector instance covers one trading day. Feed it each closed candle together with the
opening-range levels *after* the OR tracker has processed that candle:

    for candle in candles:
        levels = or_tracker.update(candle)
        result = detector.update(candle, or_tracker.orh, or_tracker.orl)

Existing machines see the candle first; machines for levels that this very candle fixed are created
afterwards, so a level never trades on the candle that created it.

The detector reports candidate signals only. Zone filter, arbitration, stops, sizing and option
selection are applied downstream (Phase 5/6).
"""

from __future__ import annotations

from datetime import date

from orb_intraday_bot.config.settings import EnabledSetups, EntriesConfig
from orb_intraday_bot.core.models import Candle, Direction
from orb_intraday_bot.core.signals import (
    PatternKind,
    Setup,
    SetupSignal,
    StepResult,
)
from orb_intraday_bot.indicators.pivots import Pivots
from orb_intraday_bot.patterns.breakout import BreakoutMachine
from orb_intraday_bot.patterns.trap import TrapMachine

_FAILURE_CASE = {PatternKind.FAILED_CASE_1: 1, PatternKind.FAILED_CASE_2: 2}


class CoreSetupDetector:
    def __init__(
        self,
        enabled: EnabledSetups,
        pivots: Pivots | None = None,
        entries: EntriesConfig | None = None,
    ) -> None:
        self._enabled = enabled
        self._tolerance = (entries or EntriesConfig()).retest_tolerance_points
        self.breakouts: dict[str, BreakoutMachine] = {}
        self.traps: list[TrapMachine] = []
        if pivots is not None:
            self.breakouts["R2"] = BreakoutMachine(
                level_name="R2", level=pivots.r2, direction=Direction.LONG,
                setup=Setup.PIVOT_BREAKOUT, target_ref="R4",
                retest_tolerance=self._tolerance,
            )
            self.breakouts["S2"] = BreakoutMachine(
                level_name="S2", level=pivots.s2, direction=Direction.SHORT,
                setup=Setup.PIVOT_BREAKOUT, target_ref="S4",
                retest_tolerance=self._tolerance,
            )
        self._day: date | None = None

    def update(self, candle: Candle, orh: float | None, orl: float | None) -> StepResult:
        day = candle.start.date()
        if self._day is None:
            self._day = day
        elif day != self._day:
            raise ValueError("CoreSetupDetector is per day; create a new instance for a new day")

        result = StepResult()

        # 1. existing breakout machines (OR levels first, then pivots; dict insertion order)
        for name, machine in list(self.breakouts.items()):
            step = machine.update(candle)
            result.events.extend(step.events)
            result.signals.extend(step.signals)
            for ev in step.events:
                case = _FAILURE_CASE.get(ev.kind)
                if case is not None and name in ("ORH", "ORL"):
                    self._start_trap(machine, case, candle, result)

        # 2. existing trap machines
        for trap in self.traps:
            step = trap.update(candle)
            result.events.extend(step.events)
            result.signals.extend(step.signals)
        self.traps = [t for t in self.traps if not t.done]

        # 3. machines for levels fixed on this candle start with the next candle
        if orh is not None and "ORH" not in self.breakouts:
            self._register("ORH", orh, Direction.LONG, Setup.ORB_BREAKOUT, "R2")
        if orl is not None and "ORL" not in self.breakouts:
            self._register("ORL", orl, Direction.SHORT, Setup.ORB_BREAKOUT, "S2")

        result.signals = [s for s in result.signals if self._is_enabled(s)]
        return result

    # -- helpers -------------------------------------------------------------------------
    def _register(
        self, name: str, level: float, direction: Direction, setup: Setup, target_ref: str
    ) -> None:
        # Insert OR machines before the pivot machines so same-candle signals list OR first.
        machine = BreakoutMachine(
            level_name=name, level=level, direction=direction, setup=setup,
            target_ref=target_ref, retest_tolerance=self._tolerance,
        )
        ordered = {name: machine, **self.breakouts}
        self.breakouts = ordered

    def _start_trap(
        self, machine: BreakoutMachine, case: int, candle: Candle, result: StepResult
    ) -> None:
        # A failed ORH breakout (LONG) is a bull trap -> SHORT; a failed ORL breakout is a bear
        # trap -> LONG. The target is the opposite OR level.
        trade_dir = Direction.SHORT if machine.direction is Direction.LONG else Direction.LONG
        target = "ORL" if machine.level_name == "ORH" else "ORH"
        trap = TrapMachine(
            level_name=machine.level_name,
            level=machine.level,
            trade_direction=trade_dir,
            case=case,
            sign_candle=candle,
            target_ref=target,
        )
        result.extend(trap.start())
        self.traps.append(trap)

    def _is_enabled(self, signal: SetupSignal) -> bool:
        cfg = self._enabled
        match signal.setup:
            case Setup.ORB_BREAKOUT | Setup.PIVOT_BREAKOUT:
                return cfg.orb
            case Setup.TRAP_REVERSE_RETEST:
                return cfg.trap
            case Setup.FOLLOWUP_CASE_1 | Setup.FOLLOWUP_CASE_2:
                return cfg.followup_failure
        return False
