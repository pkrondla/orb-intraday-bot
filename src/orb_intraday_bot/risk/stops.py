"""Stop plan in Nifty futures points (spec section 5, with the ORB level-bound rule).

- ORB / R2-S2 breakout: `price_stop` and `close_exit_level` are both the setup level.
  Distance to that level must be in [min_stop_points, hard_stop_points] (10-35);
  otherwise skip. 35 is a skip, not a nearer stop sitting in front of the level.
- touch: `stop_level` or 35 points, whichever is closer (traps, structure, fake breaks).
- none: the standard 35 points (Case 1/2, consolidation).
- other `close`: candle close beyond `stop_level`, or a hard stop of 35 points, first
  (additional whipsaw after an ORB).

`stop_points` is the futures distance used for the position-size reference and the option stop
(`option_SL = LTP - stop_points * |delta|`). For ORB / pivot-breakout it equals the distance to
the level (and matches the live stop). For touch stops it is 35, or the distance to the level if
that is closer, never below the 10-point safety floor.
"""

from __future__ import annotations

from dataclasses import dataclass

from orb_intraday_bot.config.settings import StopsConfig
from orb_intraday_bot.core.errors import TradePlanError
from orb_intraday_bot.core.models import Direction
from orb_intraday_bot.core.signals import Setup, SetupSignal

# Price stop sits on the setup level. 35 is a maximum *distance* (skip if farther), not a
# relocated stop that can fire before the level.
_LEVEL_BOUND_SETUPS = frozenset({Setup.ORB_BREAKOUT, Setup.PIVOT_BREAKOUT})


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

    if signal.setup in _LEVEL_BOUND_SETUPS:
        return _level_bound_stop(signal, dist, hard, floor)

    if signal.stop_on == "touch":
        if dist <= 0:
            raise PlanError(
                f"stop level {signal.stop_level} is not behind the entry {entry}"
            )
        points = max(floor, min(hard, dist))
        return StopPlan(points, entry - d * points, None, _basis(dist, hard, floor, "level"))

    # stop_on == "close" (additional whipsaw after an ORB / trap)
    points = max(floor, min(hard, max(dist, 0.0)))
    basis = _basis(max(dist, 0.0), hard, floor, "close level")
    if dist <= 0:
        basis += f"; entry is already beyond the level {signal.stop_level}"
    return StopPlan(points, entry - d * hard, signal.stop_level, basis)


def _level_bound_stop(
    signal: SetupSignal, dist: float, hard: float, floor: float
) -> StopPlan:
    """ORB / pivot-breakout: stop at the setup level, skip if the fill is too close or too far."""
    level = signal.stop_level
    assert level is not None
    name = signal.level_name
    if dist < floor:
        if dist <= 0:
            raise PlanError(
                f"entry {signal.entry_price} is already beyond {name} {level}"
            )
        raise PlanError(
            f"{name} is {dist:.1f} pts away: below the {floor:g}-point floor"
        )
    if dist > hard:
        raise PlanError(
            f"{name} is {dist:.1f} pts away: above the {hard:g}-point cap"
        )
    return StopPlan(dist, level, level, f"{name} is {dist:.1f} pts away")


def _basis(dist: float, hard: float, floor: float, what: str) -> str:
    if dist > hard:
        return f"{what} is {dist:.1f} pts away: hard stop {hard:g}"
    if dist < floor:
        return f"{what} is {dist:.1f} pts away: raised to the {floor:g}-point floor"
    return f"{what} is {dist:.1f} pts away"
