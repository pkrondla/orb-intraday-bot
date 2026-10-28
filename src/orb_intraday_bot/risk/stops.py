"""Stop plan in Nifty futures points (teacher notes).

- ORB / R2-S2 breakout / additional ORB whipsaw (`close`): candle close back through the
  setup level, **or** a hard stop of 35 points, whichever comes first.
- trap / structure / fake break (`touch`): the stop level or 35 points, whichever is closer.
- Case 1/2 and consolidation (`none`): the standard 35 points.

`stop_points` is the futures distance used for lot sizing and the option crash-net
(`option_SL = LTP - stop_points * |delta|`). For close-based setups that is 35 (the live
hard stop). For touch stops it is 35, or the distance to the level if that is closer,
never below the 10-point safety floor [assumption].
"""

from __future__ import annotations

from dataclasses import dataclass

from orb_intraday_bot.config.settings import StopsConfig
from orb_intraday_bot.core.errors import TradePlanError
from orb_intraday_bot.core.models import Direction
from orb_intraday_bot.core.signals import Setup, SetupSignal

_CLOSE_THROUGH_SETUPS = frozenset({Setup.ORB_BREAKOUT, Setup.PIVOT_BREAKOUT})


class PlanError(TradePlanError, ValueError):
    """The signal cannot be turned into a valid trade plan; the message is shown in the report."""


@dataclass(frozen=True, slots=True)
class StopPlan:
    stop_points: float
    price_stop: float
    close_exit_level: float | None
    basis: str


def plan_stop(signal: SetupSignal, cfg: StopsConfig) -> StopPlan:
    d = 1.0 if signal.direction is Direction.LONG else -1.0
    entry = signal.entry_price
    hard, floor = cfg.hard_stop_points, cfg.min_stop_points

    if signal.stop_on == "none" or signal.stop_level is None:
        return StopPlan(hard, entry - d * hard, None, f"standard {hard:g}-point stop")

    dist = (entry - signal.stop_level) * d          # positive: the level is behind the entry

    if signal.stop_on == "touch":
        if dist <= 0:
            raise PlanError(
                f"stop level {signal.stop_level} is not behind the entry {entry}"
            )
        points = max(floor, min(hard, dist))
        return StopPlan(points, entry - d * points, None, _basis(dist, hard, floor, "level"))

    # stop_on == "close": ORB, R2/S2 as OR, additional ORB whipsaw.
    # Notes: close back through the level, or max 35 points, whichever first.
    if signal.setup in _CLOSE_THROUGH_SETUPS and dist <= 0:
        raise PlanError(
            f"entry {entry} is already beyond {signal.level_name} {signal.stop_level}"
        )
    basis = f"close through {signal.level_name} or {hard:g}-pt hard stop"
    if dist <= 0:
        basis += f"; entry is already beyond the level {signal.stop_level}"
    return StopPlan(hard, entry - d * hard, signal.stop_level, basis)


def _basis(dist: float, hard: float, floor: float, what: str) -> str:
    if dist > hard:
        return f"{what} is {dist:.1f} pts away: hard stop {hard:g}"
    if dist < floor:
        return f"{what} is {dist:.1f} pts away: raised to the {floor:g}-point floor"
    return f"{what} is {dist:.1f} pts away"
