"""Target plan: T2 (final) and T1 (partial), in futures prices (spec section 5).

T2 comes from the signal's `target_ref`:

| target_ref               | T2                                                                  |
|--------------------------|---------------------------------------------------------------------|
| R2 / S2 / R4 / S4        | that pivot. R2/S2 with the entry already beyond it become R4/S4      |
| ORH / ORL                | that opening-range level (trap setups)                               |
| OR_PROJECTION_OR_PIVOT   | the nearer of the signal's OR-range projection and the next pivot    |
| RANGE_OPPOSITE_END       | the signal's own price (opposite end of the consolidation)           |

T2 is capped by the mandatory exits: touching R2 (S2) closes everything, so a trade entered below
R2 never targets beyond it; trades on a broken R2 (a `PIVOT_BREAKOUT`, or an entry already beyond
R2) are capped at R4 (S4). An entry beyond R4 (S4) has no room and is rejected.

Neutral zone (R1 to S1) [spec section 4]: scalp and run, every standard setup. T2 is the nearest
pivot still inside the zone (P, else R1 / S1), at least `min_stop_points` away. T1 is the nearest
level (ORH / ORL or a pivot) strictly between the entry and that T2 and at least the floor away;
otherwise halfway. R2 / S2 are not aims in this zone.

T1 [Assumption]: the nearest pivot strictly between the entry and T2, else halfway. Optionally
(targets.t1_max_distance_points, off by default) T1 is pulled in to at most that many points.

T2 is taken when the futures extreme comes within `t2_proximity_points` of the level (default 3),
so an S2/R2 rejection a few points in front still books. The chart level stays T2; the fill is
at the band (or at T2 / the open if price traded through). The buffer shrinks so T2 keeps at
least `min_stop_points` of room from the entry.

A target closer than the 10-point safety floor is rejected: the trade would have no room.
"""

from __future__ import annotations

from dataclasses import dataclass

from orb_intraday_bot.config.settings import StopsConfig
from orb_intraday_bot.core.models import Direction, Zone
from orb_intraday_bot.core.signals import Setup, SetupSignal
from orb_intraday_bot.indicators.pivots import Pivots
from orb_intraday_bot.risk.stops import PlanError


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
    if zone is Zone.NEUTRAL:
        return _neutral_targets(signal, pivots, orh, orl, stops, t1_max_points)

    long = signal.direction is Direction.LONG
    d = 1.0 if long else -1.0
    entry = signal.entry_price
    levels = pivots.levels() if pivots is not None else {}

    def need(name: str) -> float:
        if name not in levels:
            raise PlanError(f"{name} unavailable (no previous-session pivots)")
        return levels[name]

    ref = signal.target_ref
    if ref in ("R2", "S2", "R4", "S4"):
        assert ref is not None
        raw, label = need(ref), ref
        if ref in ("R2", "S2") and (raw - entry) * d <= 0:
            alt = "R4" if long else "S4"
            raw, label = need(alt), f"{alt} (entry already beyond {ref})"
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
            # the next pivot that leaves a real target (at least the 10-point floor away)
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

    t2_points = abs(raw - entry)
    if t2_points < stops.min_stop_points:
        raise PlanError(f"target {label} is only {t2_points:.1f} pts away")

    t1, t1_basis = _t1(entry, raw, pivots)
    t1, t1_basis = _cap_t1(entry, t1, t1_basis, d, t1_max_points)
    return TargetPlan(
        t1=t1,
        t2=raw,
        t1_points=abs(t1 - entry),
        t2_points=t2_points,
        t1_basis=t1_basis,
        t2_basis=label,
    )


def _neutral_targets(
    signal: SetupSignal,
    pivots: Pivots | None,
    orh: float | None,
    orl: float | None,
    stops: StopsConfig,
    t1_max_points: float | None,
) -> TargetPlan:
    """Scalp and run inside R1–S1: T2 = nearest in-zone pivot, T1 = first magnet in front."""
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
    t2_name, t2 = min(in_zone, key=lambda nv: abs(nv[1] - entry))

    t1, t1_basis = _neutral_t1(entry, t2, d, pivots, orh, orl, long, floor)
    t1, t1_basis = _cap_t1(entry, t1, t1_basis, d, t1_max_points)
    return TargetPlan(
        t1=t1,
        t2=t2,
        t1_points=abs(t1 - entry),
        t2_points=abs(t2 - entry),
        t1_basis=t1_basis,
        t2_basis=f"{t2_name} (neutral zone, nearest pivot)",
    )


def _neutral_t1(
    entry: float,
    t2: float,
    d: float,
    pivots: Pivots,
    orh: float | None,
    orl: float | None,
    long: bool,
    floor: float,
) -> tuple[float, str]:
    magnets: list[tuple[str, float]] = [
        (n, v) for n, v in pivots.levels_between(entry, t2) if abs(v - entry) >= floor
    ]
    or_name, or_level = ("ORH", orh) if long else ("ORL", orl)
    if or_level is not None:
        between = (or_level - entry) * d > 0 and (t2 - or_level) * d > 0
        if between and abs(or_level - entry) >= floor:
            magnets.append((or_name, or_level))
    if not magnets:
        return (entry + t2) / 2, "halfway to T2"
    name, value = min(magnets, key=lambda nv: abs(nv[1] - entry))
    if name in ("ORH", "ORL"):
        return value, name
    return value, f"nearest pivot {name}"


def _t1(entry: float, t2: float, pivots: Pivots | None) -> tuple[float, str]:
    if pivots is not None:
        between = pivots.levels_between(entry, t2)
        if between:
            name, value = min(between, key=lambda nv: abs(nv[1] - entry))
            return value, f"nearest pivot {name}"
    return (entry + t2) / 2, "halfway to T2"


def _cap_t1(
    entry: float,
    t1: float,
    t1_basis: str,
    d: float,
    t1_max_points: float | None,
) -> tuple[float, str]:
    if t1_max_points is not None and abs(t1 - entry) > t1_max_points:
        return entry + d * t1_max_points, f"{t1_basis}, capped at {t1_max_points:g} pts"
    return t1, t1_basis


def t2_trigger_price(
    entry: float,
    t2: float,
    *,
    long: bool,
    proximity_points: float,
    min_stop_points: float,
) -> float:
    """Futures price at which T2 fires. Equal to `t2` when proximity is 0 or T2 is too close."""
    if proximity_points <= 0:
        return t2
    t2_pts = abs(t2 - entry)
    buf = min(proximity_points, max(0.0, t2_pts - min_stop_points))
    d = 1.0 if long else -1.0
    return t2 - d * buf
