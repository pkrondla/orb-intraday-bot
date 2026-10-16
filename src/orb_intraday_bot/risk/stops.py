"""Stop plan in Nifty futures points (spec section 5).

| stop_on | stop                                                                              |
|---------|-----------------------------------------------------------------------------------|
| close   | exit on a candle close beyond `stop_level`, or a hard stop of 35 points, first    |
| touch   | `stop_level` or 35 points, whichever is closer                                     |
| none    | the standard 35 points                                                             |

`stop_points` is the futures distance used for the position-size reference and the option stop
(`option_SL = LTP - stop_points * |delta|`). It is 35 points, or the distance to the level if that
is closer, never below the 10-point safety floor.

The `close` stop has no price: `close_exit_level` is checked at candle close, and `price_stop`
is the 35-point hard stop behind it.
[Concern] Sizing on the distance to the level while the hard stop sits 35 points away means the
worst-case loss on a `close` setup can be larger than the risk budget (the option stop, which uses
`stop_points`, normally exits earlier). This follows the spec; see intraday-setup.md section 10.
"""

from __future__ import annotations

from dataclasses import dataclass

from orb_intraday_bot.config.settings import StopsConfig
from orb_intraday_bot.core.models import Direction
from orb_intraday_bot.core.signals import SetupSignal


class PlanError(ValueError):
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

    # stop_on == "close"
    points = max(floor, min(hard, max(dist, 0.0)))
    basis = _basis(max(dist, 0.0), hard, floor, "close level")
    if dist <= 0:
        basis += f"; entry is already beyond the level {signal.stop_level}"
    return StopPlan(points, entry - d * hard, signal.stop_level, basis)


def _basis(dist: float, hard: float, floor: float, what: str) -> str:
    if dist > hard:
        return f"{what} is {dist:.1f} pts away: hard stop {hard:g}"
    if dist < floor:
        return f"{what} is {dist:.1f} pts away: raised to the {floor:g}-point floor"
    return f"{what} is {dist:.1f} pts away"
