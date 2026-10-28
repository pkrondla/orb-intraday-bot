"""One trading day, candle by candle. Shared by the live runner and the replay command.

For each closed candle, in this order:

1. open positions see the candle (exits, end of day), so a new signal can never act on it
2. the opening-range tracker and the setup detectors process it
3. the signal engine plans and arbitrates the candidate signals
4. each accepted trade gets an option plan and opens a paper position

Everything is idempotent against the store: a signal that was already handled (accepted,
rejected or released) is never opened again, and a signal that already has a position is not
planned twice. That is what makes a restart's catch-up replay of the day safe.

A signal is only opened if `allow_open` is true (it is fresh). A signal that fires while the bot
was not running is recorded as rejected ("stale") and never opened at an old price.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from orb_intraday_bot.config.settings import StrategyConfig
from orb_intraday_bot.core.errors import EntryNotFilledError
from orb_intraday_bot.core.events import LevelSet
from orb_intraday_bot.core.models import Candle, Zone
from orb_intraday_bot.core.signals import PatternEvent, SetupSignal
from orb_intraday_bot.engine.signal_engine import PlannedTrade, SignalEngine
from orb_intraday_bot.indicators.opening_range import OpeningRangeTracker
from orb_intraday_bot.indicators.pivots import Pivots
from orb_intraday_bot.options.planner import OptionPlanner
from orb_intraday_bot.options.pricing import OptionPlan, OptionPlanError
from orb_intraday_bot.options.selector import OptionSelectionError
from orb_intraday_bot.paper.engine import PaperEngine, PositionHook
from orb_intraday_bot.paper.exits import ExitEvent
from orb_intraday_bot.paper.models import Position
from orb_intraday_bot.paper.ports import PositionExecutor
from orb_intraday_bot.paper.report import DaySummary, summarize
from orb_intraday_bot.patterns.detector import SetupDetector
from orb_intraday_bot.storage.store import TradeStore


@dataclass(frozen=True, slots=True)
class SignalOutcome:
    signal: SetupSignal
    status: str                 # opened | rejected | stale | skipped
    detail: str = ""
    stage: str = ""
    trade: PlannedTrade | None = None
    plan: OptionPlan | None = None
    position: Position | None = None


@dataclass(slots=True)
class CandleReport:
    candle: Candle
    symbol: str
    orh: float | None
    orl: float | None
    zone: Zone
    pivots: Pivots | None
    level_sets: list[LevelSet] = field(default_factory=list)
    events: list[PatternEvent] = field(default_factory=list)
    outcomes: list[SignalOutcome] = field(default_factory=list)
    exits: list[ExitEvent] = field(default_factory=list)
    marks: list[tuple[Position, float]] = field(default_factory=list)
    summary: DaySummary | None = None
    catch_up: bool = False
    trend: str = "none"

    @property
    def opened(self) -> list[SignalOutcome]:
        return [o for o in self.outcomes if o.status == "opened"]


class TradingDay:
    def __init__(
        self,
        config: StrategyConfig,
        day: date,
        symbol: str,
        pivots: Pivots | None,
        planner: OptionPlanner,
        store: TradeStore | None = None,
        *,
        executor: PositionExecutor | None = None,
        executor_enabled: Callable[[], bool] | None = None,
        on_open: PositionHook | None = None,
        on_close: PositionHook | None = None,
    ) -> None:
        self._cfg = config
        self.day = day
        self.symbol = symbol
        self.pivots = pivots
        self._planner = planner
        self._store = store
        self.tracker = OpeningRangeTracker()
        self.detector = SetupDetector(config, pivots)
        self.signals = SignalEngine(config, pivots)
        self.paper = PaperEngine(
            config, store, executor, executor_enabled, on_open, on_close
        )
        self.last_candle: Candle | None = None

    # -- candles -------------------------------------------------------------------------
    def on_candle(
        self, candle: Candle, *, allow_open: bool = True, catch_up: bool = False
    ) -> CandleReport:
        option_ltps = None       # live premiums are supplied by tick polls; candles are modelled
        report = CandleReport(
            candle=candle, symbol=self.symbol, orh=None, orl=None, zone=Zone.NEUTRAL,
            pivots=self.pivots, catch_up=catch_up,
        )
        report.exits = self.paper.on_candle(candle, option_ltps)

        report.level_sets = self.tracker.update(candle)
        report.orh, report.orl = self.tracker.orh, self.tracker.orl
        step = self.detector.update(candle, report.orh, report.orl)
        report.events = step.events
        report.trend = self.detector.trending.state

        decision = self.signals.evaluate(
            candle, step.signals, report.orh, report.orl, self.paper.open_directions()
        )
        report.zone = decision.zone
        for rejection in decision.rejected:
            if self._store is not None:
                self._store.record_signal(
                    rejection.signal, "rejected", rejection.stage, rejection.reason
                )
            report.outcomes.append(
                SignalOutcome(rejection.signal, "rejected", rejection.reason, rejection.stage)
            )
        for trade in decision.accepted:
            report.outcomes.append(self._open(trade, candle, allow_open))

        report.marks = self.paper.mark_to_market(candle.close)
        report.summary = summarize(self.day, self.positions_today())
        self.last_candle = candle
        return report

    def _open(self, trade: PlannedTrade, candle: Candle, allow_open: bool) -> SignalOutcome:
        sig = trade.signal
        status = self._store.signal_status(sig.signal_id) if self._store is not None else None
        if status is not None and status != "accepted":
            # Handled in an earlier run (rejected, released, stale): never open it now.
            self.signals.release(trade)
            return SignalOutcome(sig, "skipped", f"already {status} earlier", "", trade)
        if self.paper.has_position(sig.signal_id):
            return SignalOutcome(sig, "skipped", "position already exists", "", trade)
        if status is None and self._store is not None:
            self._store.record_signal(sig, "accepted")
        if not allow_open:
            self.signals.release(trade)
            if self._store is not None:
                self._store.set_signal_status(
                    sig.signal_id, "rejected", "stale", "the bot was not running at the time"
                )
            return SignalOutcome(
                sig, "stale", "signal is too old to enter at its price", "stale", trade
            )
        try:
            plan = self._planner.plan(trade, self.day)
        except (OptionPlanError, OptionSelectionError) as exc:
            self.signals.release(trade)
            if self._store is not None:
                self._store.set_signal_status(sig.signal_id, "released", "option", str(exc))
            return SignalOutcome(sig, "rejected", str(exc), "option", trade)
        try:
            position = self.paper.open(trade, plan, candle.start + timedelta(minutes=3))
        except EntryNotFilledError as exc:
            # The buy order did not happen (risk gate, rejected, not filled in time). Nothing is
            # open at the broker; the signal is released and never entered late.
            self.signals.release(trade)
            if self._store is not None:
                self._store.set_signal_status(sig.signal_id, "released", "entry", str(exc))
            return SignalOutcome(sig, "rejected", str(exc), "entry", trade, plan)
        if position is None:
            return SignalOutcome(sig, "skipped", "position already exists", "", trade, plan)
        return SignalOutcome(sig, "opened", "", "", trade, plan, position)

    # -- live prices ---------------------------------------------------------------------
    def positions_today(self) -> list[Position]:
        if self._store is not None:
            return self._store.positions_for_day(self.day)
        return self.paper.open_positions + self.paper.closed_positions

    def summary(self) -> DaySummary:
        return summarize(self.day, self.positions_today())
