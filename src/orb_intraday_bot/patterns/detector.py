"""All pattern detectors for one trading day (Phases 3 and 4).

Feed it each closed candle, with the OR levels after the OR tracker has processed that candle:

    detector = SetupDetector(cfg, pivots)
    for candle in candles:
        tracker.update(candle)
        result = detector.update(candle, tracker.orh, tracker.orl)

`result.signals` are candidate trades; `result.events` are pattern state changes for the report.
The signals still need the zone filter, arbitration, stops, sizing and option selection (later
phases). Signals that share a `sign_id` are alternatives: only one trade is taken per sign.
"""

from __future__ import annotations

from datetime import date

from orb_intraday_bot.config.settings import StrategyConfig
from orb_intraday_bot.core.models import Candle, Direction
from orb_intraday_bot.core.signals import PatternKind, Setup, SetupSignal, StepResult
from orb_intraday_bot.indicators.pivots import Pivots
from orb_intraday_bot.patterns.consolidation import ConsolidationMachine
from orb_intraday_bot.patterns.core_setups import CoreSetupDetector
from orb_intraday_bot.patterns.fake_break import FakeBreakMachine
from orb_intraday_bot.patterns.structure import StructureWhipsawMachine
from orb_intraday_bot.patterns.trending import TrendingDayTracker
from orb_intraday_bot.patterns.whipsaw import RejectionWhipsawMachine


class SetupDetector:
    def __init__(self, config: StrategyConfig, pivots: Pivots | None = None) -> None:
        if config.enabled_setups.both_sides_trap:
            raise NotImplementedError(
                "both_sides_trap: the notes give the title only, with no rules. Keep it off."
            )
        self._cfg = config
        self.core = CoreSetupDetector(config.enabled_setups, pivots, config.entries)
        self.whipsaws: list[RejectionWhipsawMachine] = []
        self.consolidations: dict[str, ConsolidationMachine] = {}
        self.trending = TrendingDayTracker()
        sc = config.structure
        self.structure = [
            StructureWhipsawMachine(
                direction=d,
                min_count=sc.min_count,
                max_count=sc.max_count,
                min_rise_points=sc.min_rise_points,
            )
            for d in (Direction.LONG, Direction.SHORT)
        ]
        fb = config.fake_break
        self.fake_breaks = [
            FakeBreakMachine(
                direction=d,
                consolidation_candles=fb.consolidation_candles,
                max_range_points=fb.max_range_points,
                edge_touches=fb.edge_touches,
                max_fake_candles=fb.max_fake_candles,
                min_reversal_leg_points=fb.min_reversal_leg_points,
                max_wait_candles=fb.max_wait_candles,
                retracement=config.entries.fake_breakout_entry_retracement,
            )
            for d in (Direction.LONG, Direction.SHORT)
        ]
        self._day: date | None = None

    def update(self, candle: Candle, orh: float | None, orl: float | None) -> StepResult:
        day = candle.start.date()
        if self._day is None:
            self._day = day
        elif day != self._day:
            raise ValueError("SetupDetector is per day; create a new instance for a new day")

        result = StepResult()

        # 1. machines that existed before this candle
        for machine in self.whipsaws:
            result.extend(machine.update(candle))
        self.whipsaws = [m for m in self.whipsaws if not m.done]
        for cons in self.consolidations.values():
            result.extend(cons.update(candle, orh, orl))
        for st in self.structure:
            result.extend(st.update(candle))
        for fb in self.fake_breaks:
            result.extend(fb.update(candle))

        # 2. the core ORB / trap / follow-up detector
        core = self.core.update(candle, orh, orl)
        result.extend(core)

        # 3. machines started by what happened on this candle (they see the next candle)
        self._start_whipsaws(core)
        cc = self._cfg.consolidation
        tol = self._cfg.entries.retest_tolerance_points
        for name, level, direction in (
            ("ORH", orh, Direction.LONG),
            ("ORL", orl, Direction.SHORT),
        ):
            if level is not None and name not in self.consolidations:
                self.consolidations[name] = ConsolidationMachine(
                    level_name=name,
                    level=level,
                    direction=direction,
                    min_touches=cc.min_touches,
                    min_minutes=cc.min_minutes,
                    max_minutes=cc.max_minutes,
                    tolerance=tol,
                )

        # 4. gating, trending-day label
        result.signals = [s for s in result.signals if self._is_enabled(s)]
        result.events.extend(self.trending.update(candle, result.events, result.signals))
        return result

    # -- helpers -------------------------------------------------------------------------
    def _start_whipsaws(self, core: StepResult) -> None:
        tol = self._cfg.entries.retest_tolerance_points
        minimum = self._cfg.whipsaw.min_rejections
        for ev in core.events:
            if ev.level_name not in ("ORH", "ORL"):
                continue
            if ev.kind is PatternKind.CONFIRMED:
                parent, stop_on = "ORB", "close"
                target = "R2" if ev.direction is Direction.LONG else "S2"
            elif ev.kind is PatternKind.TRAP_TRIGGERED:
                parent, stop_on = "TRAP", "touch"
                target = "ORH" if ev.direction is Direction.LONG else "ORL"
            else:
                continue
            level = self.core.breakouts[ev.level_name].level
            self.whipsaws.append(
                RejectionWhipsawMachine(
                    level_name=ev.level_name,
                    level=level,
                    direction=ev.direction,
                    parent=parent,
                    stop_on="close" if stop_on == "close" else "touch",
                    target_ref=target,
                    min_rejections=minimum,
                    tolerance=tol,
                    parent_candle=ev.candle_start,
                )
            )

    def _is_enabled(self, signal: SetupSignal) -> bool:
        cfg = self._cfg.enabled_setups
        match signal.setup:
            case Setup.ADDITIONAL_WHIPSAW:
                return cfg.additional_whipsaw
            case Setup.CONSOLIDATION_BREAKOUT:
                return cfg.consolidation_breakout
            case Setup.STRUCTURE_WHIPSAW:
                return cfg.whipsaw_structure
            case Setup.FAKE_BREAK:
                return cfg.fake_breakout
        return True   # core setups were already gated by CoreSetupDetector
