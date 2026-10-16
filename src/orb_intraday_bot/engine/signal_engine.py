"""Signal engine: turns candidate signals into planned trades, or says why not.

Per candle, for each candidate signal (from `SetupDetector`):

1. one trade per sign: skipped if its `sign_id` was already traded (a blocked signal does not use
   up its sign, so the standard trigger of the same sign can still come later)
2. zone filter (hard rules)
3. stop plan and target plan (rejects signals with no room, a bad stop level, no pivots, ...)
4. arbitration among the survivors, highest priority first:
   - a lower-priority signal in the opposite direction on the same candle is dropped
   - an open position in the opposite direction blocks a new trade (unless allowed by config)
   - maximum open positions, trades per day, and 2LL/3LL/2HH/3HH trades per day

Every candidate ends up either in `accepted` (a `PlannedTrade`) or in `rejected` (with the stage
and reason), so the per-candle report can say what was blocked and why.

The engine is per day. It does no I/O and has no clock. Option selection, delta pricing and lot
sizing happen after this (Phase 6), and the paper engine opens the position (Phase 7).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from orb_intraday_bot.config.settings import StrategyConfig
from orb_intraday_bot.core.models import Candle, Direction, Zone
from orb_intraday_bot.core.signals import Setup, SetupSignal
from orb_intraday_bot.engine.zone_filter import zone_block_reason
from orb_intraday_bot.indicators.pivots import Pivots
from orb_intraday_bot.indicators.zones import classify_zone
from orb_intraday_bot.risk.sizing import risk_budget_inr
from orb_intraday_bot.risk.stops import PlanError, StopPlan, plan_stop
from orb_intraday_bot.risk.targets import TargetPlan, plan_targets


@dataclass(frozen=True, slots=True)
class PlannedTrade:
    signal: SetupSignal
    zone: Zone
    stop: StopPlan
    targets: TargetPlan
    risk_budget_inr: float


@dataclass(frozen=True, slots=True)
class Rejection:
    signal: SetupSignal
    stage: str       # "duplicate", "zone", "plan", "arbiter"
    reason: str


@dataclass(slots=True)
class EngineResult:
    zone: Zone
    accepted: list[PlannedTrade]
    rejected: list[Rejection]


class SignalEngine:
    def __init__(self, config: StrategyConfig, pivots: Pivots | None) -> None:
        self._cfg = config
        self._pivots = pivots
        self.taken_signs: set[str] = set()
        self.trades_today = 0
        self.structure_trades_today = 0

    def zone_for(self, price: float) -> Zone:
        if self._pivots is None:
            return Zone.NEUTRAL
        return classify_zone(price, self._pivots)

    def evaluate(
        self,
        candle: Candle,
        signals: Sequence[SetupSignal],
        orh: float | None,
        orl: float | None,
        open_directions: Sequence[Direction] = (),
    ) -> EngineResult:
        zone = self.zone_for(candle.close)
        result = EngineResult(zone=zone, accepted=[], rejected=[])
        survivors: list[PlannedTrade] = []

        for sig in signals:
            if sig.sign_id in self.taken_signs:
                result.rejected.append(Rejection(sig, "duplicate", "sign already traded"))
                continue
            reason = zone_block_reason(sig, zone)
            if reason is not None:
                result.rejected.append(Rejection(sig, "zone", reason))
                continue
            try:
                stop = plan_stop(sig, self._cfg.stops)
                targets = plan_targets(
                    sig, self._pivots, orh, orl, self._cfg.stops,
                    self._cfg.targets.t1_max_distance_points,
                )
            except PlanError as exc:
                result.rejected.append(Rejection(sig, "plan", str(exc)))
                continue
            survivors.append(
                PlannedTrade(sig, zone, stop, targets, risk_budget_inr(self._cfg.sizing))
            )

        survivors.sort(key=self._priority)
        open_dirs = list(open_directions)
        for trade in survivors:
            reason = self._arbiter_block(trade, result.accepted, open_dirs)
            if reason is not None:
                result.rejected.append(Rejection(trade.signal, "arbiter", reason))
                continue
            result.accepted.append(trade)
            self.taken_signs.add(trade.signal.sign_id)
            self.trades_today += 1
            if trade.signal.setup is Setup.STRUCTURE_WHIPSAW:
                self.structure_trades_today += 1
        return result

    def release(self, trade: PlannedTrade) -> None:
        """Undo the bookkeeping for an accepted trade that could not be opened (for example the
        option plan failed), so its sign and the daily counters are not used up."""
        if trade.signal.sign_id in self.taken_signs:
            self.taken_signs.discard(trade.signal.sign_id)
            self.trades_today = max(0, self.trades_today - 1)
            if trade.signal.setup is Setup.STRUCTURE_WHIPSAW:
                self.structure_trades_today = max(0, self.structure_trades_today - 1)

    # -- helpers -------------------------------------------------------------------------
    def _priority(self, trade: PlannedTrade) -> int:
        order = self._cfg.arbiter.priority
        name = trade.signal.setup.value
        return order.index(name) if name in order else len(order)

    def _arbiter_block(
        self,
        trade: PlannedTrade,
        accepted: list[PlannedTrade],
        open_dirs: list[Direction],
    ) -> str | None:
        arb = self._cfg.arbiter
        sig = trade.signal
        opposite = Direction.SHORT if sig.direction is Direction.LONG else Direction.LONG

        if any(t.signal.direction is opposite for t in accepted):
            return "conflicts with a higher-priority signal on the same candle"
        if not arb.allow_opposite_positions and opposite in open_dirs:
            return f"a {opposite.value} position is open"
        if len(open_dirs) + len(accepted) >= self._cfg.sizing.max_open_positions:
            return f"maximum {self._cfg.sizing.max_open_positions} open positions"
        if self.trades_today >= arb.max_trades_per_day:
            return f"daily limit of {arb.max_trades_per_day} trades reached"
        if (
            sig.setup is Setup.STRUCTURE_WHIPSAW
            and self.structure_trades_today >= arb.max_structure_trades_per_day
        ):
            return f"daily limit of {arb.max_structure_trades_per_day} 2LL/3LL/2HH/3HH trades"
        return None
