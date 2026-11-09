"""Target plan: T1 is the notes Target; T2 is a one-lot runner.

T1 comes from the signal's `target_ref`:

| target_ref               | T1                                                                  |
|--------------------------|---------------------------------------------------------------------|
| R2 / S2                  | that pivot. A fill already beyond it uses the next pivot (see below) |
| R4 / S4                  | the next pivot in front; the one after it when the next is inside 1R |
| ORH / ORL                | that opening-range level (bear trap / bull trap)                     |
| OR_PROJECTION_OR_PIVOT   | the nearer of the signal's OR-range projection and the next pivot    |
| RANGE_OPPOSITE_END       | the signal's own price (opposite end of the consolidation)           |

T2 is T1 ± OR-range (ORH − ORL), or the nearer pivot still beyond T1, capped at R4/S4
(R5/S5 when T1 is already R4/S4). Neutral-zone T2 is capped at R1/S1. If there is no
room (OR unset, or closer than `min_stop_points`), T2 = T1 and the whole position
closes at T1.

A 2HH/3HH short or 2LL/3LL long does not use the neutral-zone scalp. Above ORH or
below ORL, T1 is the nearest pivot at least `hard_stop_points` (35) in front.
Inside the range, ORL (short) or ORH (long) joins that list. A nearer line inside
35 points is skipped. T2 is the next pivot beyond T1. S2/R2 still close the
position, so a target beyond them is pulled back to that level.

T1 is capped by the mandatory exits: a trade entered below R2 never has T1 beyond it.
A broken R2 (a `PIVOT_BREAKOUT`, or an entry already beyond R2) takes the next pivot
in front of the fill. When that pivot is closer than 1R (the hard stop, 35 points),
T1 is the pivot after it. R4 (S4) still closes the trade, so T1 is never beyond it.
An entry beyond R4 (S4) has no room and is rejected. The runner may go past R2/S2
because T1 already booked the target.

Neutral zone (R1 to S1): a setup whose notes name a target keeps that target. The others
take the nearest pivot still inside the zone (P, else R1 / S1). R2 / S2 are not aims
for those pivot targets.

Both levels fire when the futures extreme comes within `t2_proximity_points` (default 3).

Booking: 1 lot closes all at T1; 2+ lots leave one runner for T2.

A T1 closer than the 10-point safety floor is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass

from orb_intraday_bot.config.settings import StopsConfig
from orb_intraday_bot.core.models import Direction, Zone
from orb_intraday_bot.core.signals import Setup, SetupSignal
from orb_intraday_bot.core.ticks import futures_quote
from orb_intraday_bot.indicators.pivots import Pivots
from orb_intraday_bot.risk.stops import PlanError

# The notes name these targets. Anything else is a pivot target.
_NOTES_TARGET_SETUPS = frozenset({
    Setup.TRAP_REVERSE_RETEST,      # bear trap ORH, bull trap ORL
    Setup.CONSOLIDATION_BREAKOUT,   # OR-range projection or the nearer pivot
    Setup.FAKE_BREAK,               # opposite end of the consolidation
    Setup.PIVOT_BREAKOUT,           # R4 / S4, then the next-pivot rule
})


@dataclass(frozen=True, slots=True)
class TargetPlan:
    t1: float
    t2: float
    t1_points: float
    t2_points: float
    t1_basis: str
    t2_basis: str


def plan_targets(
    signal: SetupSignal,
    pivots: Pivots | None,
    orh: float | None,
    orl: float | None,
    stops: StopsConfig,
    t1_max_points: float | None = None,
    zone: Zone | None = None,
) -> TargetPlan:
    _ = t1_max_points  # retired: T1 is the notes target, not a pulled-in scalp
    if signal.setup is Setup.STRUCTURE_WHIPSAW:
        return _structure_targets(signal, pivots, orh, orl, stops)
    if zone is Zone.NEUTRAL and signal.setup not in _NOTES_TARGET_SETUPS:
        t1_plan = _neutral_t1(signal, pivots, stops)
        return _attach_runner(
            entry=signal.entry_price, t1=t1_plan.t1, t1_label=t1_plan.t1_basis,
            long=signal.direction is Direction.LONG, floor=stops.min_stop_points,
            orh=orh, orl=orl, pivots=pivots, setup=signal.setup, zone=zone,
        )

    long = signal.direction is Direction.LONG
    d = 1.0 if long else -1.0
    entry = signal.entry_price
    levels = pivots.levels() if pivots is not None else {}

    def need(name: str) -> float:
        if name not in levels:
            raise PlanError(f"{name} unavailable (no previous-session pivots)")
        return levels[name]

    ref = signal.target_ref
    # Case 1/2 and a trap whipsaw carry the opposite OR level, which the notes do not
    # name as their target. Their T1 is the pivot.
    if signal.setup not in _NOTES_TARGET_SETUPS and ref in ("ORH", "ORL"):
        ref = "R2" if long else "S2"
    next_pivot = signal.setup is Setup.PIVOT_BREAKOUT
    if ref in ("R2", "S2", "R4", "S4"):
        assert ref is not None
        raw, label = need(ref), ref
        if ref in ("R2", "S2") and (raw - entry) * d <= 0:
            next_pivot = True
        if next_pivot:
            if pivots is None:
                raise PlanError("next-pivot target needs previous-session pivots")
            raw, label = _t1_next_pivot(entry, d, pivots, stops.hard_stop_points)
    elif ref in ("ORH", "ORL"):
        value = orh if ref == "ORH" else orl
        if value is None:
            raise PlanError(f"{ref} is not set yet")
        raw, label = value, ref
    elif ref == "OR_PROJECTION_OR_PIVOT":
        candidates: list[tuple[float, str]] = []
        if signal.target_price is not None:
            candidates.append((signal.target_price, "OR range projection"))
        if pivots is not None:
            beyond = [
                (n, v) for n, v in pivots.ordered()
                if (v - entry) * d >= stops.min_stop_points
            ]
            if beyond:
                name, value = min(beyond, key=lambda nv: abs(nv[1] - entry))
                candidates.append((value, f"pivot {name}"))
        candidates = [c for c in candidates if (c[0] - entry) * d > 0]
        if not candidates:
            raise PlanError("no target beyond the entry")
        raw, label = min(candidates, key=lambda c: abs(c[0] - entry))
        label = f"nearer of projection and pivot: {label}"
    elif ref == "RANGE_OPPOSITE_END":
        if signal.target_price is None:
            raise PlanError("signal carries no range target")
        raw, label = signal.target_price, "opposite end of the consolidation"
    else:
        raise PlanError(f"unknown target reference {ref!r}")

    if (raw - entry) * d <= 0:
        raise PlanError(f"target {label} {raw:.1f} is behind the entry {entry:.1f}")

    if pivots is not None:
        if signal.setup is Setup.PIVOT_BREAKOUT:
            ceiling_name = "R4" if long else "S4"
        else:
            first = "R2" if long else "S2"
            ceiling_name = first if (levels[first] - entry) * d > 0 else ("R4" if long else "S4")
        ceiling = levels[ceiling_name]
        if (ceiling - entry) * d <= 0:
            raise PlanError(f"entry {entry:.1f} is beyond {ceiling_name}: no room")
        if (raw - ceiling) * d > 0:
            raw, label = ceiling, f"{label}, capped at {ceiling_name}"
        if ceiling_name in ("R2", "S2"):
            raw, label = _merge_or_with_cap(
                raw, label, ref, ceiling_name, levels[ceiling_name],
                entry, d, stops.min_stop_points,
            )

    t1_plan = _plan(entry, raw, raw, label, label, stops.min_stop_points)
    return _attach_runner(
        entry=entry, t1=t1_plan.t1, t1_label=t1_plan.t1_basis,
        long=long, floor=stops.min_stop_points, orh=orh, orl=orl, pivots=pivots,
        setup=signal.setup, zone=zone,
    )


def _structure_targets(
    signal: SetupSignal,
    pivots: Pivots | None,
    orh: float | None,
    orl: float | None,
    stops: StopsConfig,
) -> TargetPlan:
    """2HH/3HH and 2LL/3LL targets from where the fill sits relative to the opening range.

    Outside the range, T1 is the nearest pivot at least the hard stop (35) in front.
    Inside, the opening-range edge in the trade direction is a candidate too. T2 is
    the next pivot beyond T1. A line closer than the hard stop is skipped.
    """
    if pivots is None:
        raise PlanError("structure targets need previous-session pivots")
    long = signal.direction is Direction.LONG
    d = 1.0 if long else -1.0
    entry = futures_quote(signal.entry_price)
    room = stops.hard_stop_points
    levels = pivots.levels()
    # Quote first, then keep one label per price. The OR edge wins a tie with a pivot.
    chosen: dict[float, str] = {}
    for name, value in pivots.ordered():
        price = futures_quote(value)
        if (price - entry) * d >= room:
            chosen.setdefault(price, name)
    if _opening_range_side(entry, orh, orl) == "inside":
        edge_name, edge = ("ORH", orh) if long else ("ORL", orl)
        if edge is not None:
            price = futures_quote(edge)
            if (price - entry) * d >= room:
                chosen[price] = edge_name
    if not chosen:
        raise PlanError(
            f"no pivot at least {room:g} pts in front of the entry {entry:.1f}"
        )
    t1 = min(chosen, key=lambda price: (price - entry) * d)
    t1_label = chosen[t1]

    first = "R2" if long else "S2"
    ceiling_name = first if (levels[first] - entry) * d > 0 else ("R4" if long else "S4")
    ceiling = futures_quote(levels[ceiling_name])
    if (ceiling - entry) * d <= 0:
        raise PlanError(f"entry {entry:.1f} is beyond {ceiling_name}: no room")
    if (t1 - ceiling) * d > 0:
        t1, t1_label = ceiling, ceiling_name

    beyond = [
        (name, futures_quote(value))
        for name, value in pivots.ordered()
        if (futures_quote(value) - t1) * d > 0
    ]
    if not beyond:
        return _plan(entry, t1, t1, t1_label, t1_label, stops.min_stop_points)
    t2_label, t2 = min(beyond, key=lambda item: (item[1] - t1) * d)
    if (t2 - ceiling) * d > 0:
        if (ceiling - t1) * d > 0:
            t2, t2_label = ceiling, ceiling_name
        else:
            t2, t2_label = t1, t1_label
    return _plan(entry, t1, t2, t1_label, t2_label, stops.min_stop_points)


def _t1_next_pivot(
    entry: float, d: float, pivots: Pivots, one_r: float,
) -> tuple[float, str]:
    """Nearest pivot in front of the fill. Inside 1R, take the pivot after that one."""
    entry = futures_quote(entry)
    ahead = [
        (name, futures_quote(value))
        for name, value in pivots.ordered()
        if (futures_quote(value) - entry) * d > 0
    ]
    if not ahead:
        raise PlanError(f"no pivot in front of the entry {entry:.1f}")
    name, value = min(ahead, key=lambda item: (item[1] - entry) * d)
    if (value - entry) * d + 1e-9 < one_r:
        rest = [item for item in ahead if (item[1] - value) * d > 0]
        if rest:
            name, value = min(rest, key=lambda item: (item[1] - entry) * d)
    return value, name


def _opening_range_side(
    entry: float, orh: float | None, orl: float | None,
) -> str:
    """'above' past ORH, 'below' past ORL, 'inside' when both edges bracket the fill."""
    if orh is not None and entry > futures_quote(orh):
        return "above"
    if orl is not None and entry < futures_quote(orl):
        return "below"
    if orh is not None and orl is not None:
        return "inside"
    return "unknown"


def _neutral_t1(
    signal: SetupSignal,
    pivots: Pivots | None,
    stops: StopsConfig,
) -> TargetPlan:
    """Scalp inside R1–S1: T1 = nearest in-zone pivot."""
    if pivots is None:
        raise PlanError("neutral-zone targets need previous-session pivots")

    long = signal.direction is Direction.LONG
    d = 1.0 if long else -1.0
    entry = signal.entry_price
    floor = stops.min_stop_points
    edge_name, edge = ("R1", pivots.r1) if long else ("S1", pivots.s1)

    in_zone = [
        (n, v)
        for n, v in pivots.ordered()
        if (v - entry) * d >= floor and (edge - v) * d >= 0
    ]
    if not in_zone:
        raise PlanError(
            f"neutral zone: no pivot at least {floor:g} pts toward {edge_name}"
        )
    name, value = min(in_zone, key=lambda nv: abs(nv[1] - entry))
    return _plan(entry, value, value, f"{name} (neutral zone, nearest pivot)",
                 f"{name} (neutral zone, nearest pivot)", floor)


def _attach_runner(
    *,
    entry: float,
    t1: float,
    t1_label: str,
    long: bool,
    floor: float,
    orh: float | None,
    orl: float | None,
    pivots: Pivots | None,
    setup: Setup,
    zone: Zone | None,
) -> TargetPlan:
    """T2 = T1 ± OR-range, or the nearer pivot beyond T1, else T1 (no runner)."""
    d = 1.0 if long else -1.0
    collapsed = _plan(entry, t1, t1, t1_label, t1_label, floor)

    width = (orh - orl) if orh is not None and orl is not None and orh > orl else None
    candidates: list[tuple[float, str]] = []
    if width is not None and width >= floor:
        sign = "+" if long else "−"
        candidates.append((t1 + d * width, f"T1 {sign} OR-range {width:g}"))
    if pivots is not None:
        beyond = [
            (n, v) for n, v in pivots.ordered()
            if (v - t1) * d >= floor
        ]
        if beyond:
            name, value = min(beyond, key=lambda nv: abs(nv[1] - t1))
            candidates.append((value, f"pivot {name}"))
    candidates = [c for c in candidates if (c[0] - t1) * d >= floor]
    if not candidates:
        return collapsed

    raw, label = min(candidates, key=lambda c: abs(c[0] - t1))
    ceiling_name, ceiling = _runner_ceiling(long, setup, t1, pivots, zone)
    if ceiling is not None and ceiling_name is not None:
        if (ceiling - t1) * d < floor:
            return collapsed
        if (raw - ceiling) * d > 0:
            raw, label = ceiling, f"{label}, capped at {ceiling_name}"
    if (raw - t1) * d < floor:
        return collapsed
    return _plan(entry, t1, raw, t1_label, label, floor)


def _runner_ceiling(
    long: bool,
    setup: Setup,
    t1: float,
    pivots: Pivots | None,
    zone: Zone | None,
) -> tuple[str | None, float | None]:
    if pivots is None:
        return None, None
    levels = pivots.levels()
    d = 1.0 if long else -1.0
    if zone is Zone.NEUTRAL:
        name = "R1" if long else "S1"
        return name, levels[name]
    far = "R5" if long else "S5"
    mid = "R4" if long else "S4"
    if setup is Setup.PIVOT_BREAKOUT or (mid in levels and (t1 - levels[mid]) * d >= -1e-9):
        return far, levels[far]
    return mid, levels[mid]


def _plan(
    entry: float, t1: float, t2: float, t1_label: str, t2_label: str, floor: float,
) -> TargetPlan:
    entry, t1, t2 = futures_quote(entry), futures_quote(t1), futures_quote(t2)
    points = abs(t1 - entry)
    if points < floor:
        raise PlanError(f"target {t1_label} is only {points:.1f} pts away")
    return TargetPlan(
        t1=t1,
        t2=t2,
        t1_points=points,
        t2_points=abs(t2 - entry),
        t1_basis=t1_label,
        t2_basis=t2_label,
    )


def _merge_or_with_cap(
    raw: float,
    label: str,
    ref: str | None,
    cap_name: str,
    cap: float,
    entry: float,
    d: float,
    floor: float,
) -> tuple[float, str]:
    """ORL sitting on S2 (or ORH on R2) is one magnet."""
    if ref not in ("ORH", "ORL") or (cap - entry) * d <= 0:
        return raw, label
    if abs(raw - cap) > floor:
        return raw, label
    if "≈" in label or "capped at" in label:
        return raw, label
    return raw, f"{label} ≈ {cap_name}"


def t2_trigger_price(
    entry: float,
    t2: float,
    *,
    long: bool,
    proximity_points: float,
    min_stop_points: float,
) -> float:
    """Futures price at which a target fires, on the 0.1 quote.

    Equal to the level when proximity is 0 or the target is too close.
    """
    entry, t2 = futures_quote(entry), futures_quote(t2)
    if proximity_points <= 0:
        return t2
    t2_pts = abs(t2 - entry)
    buf = min(proximity_points, max(0.0, t2_pts - min_stop_points))
    d = 1.0 if long else -1.0
    return futures_quote(t2 - d * buf)
