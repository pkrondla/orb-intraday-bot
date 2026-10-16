from __future__ import annotations

import pytest

from orb_intraday_bot.core.models import Zone
from orb_intraday_bot.indicators.pivots import (
    pivots_from_session,
    session_ohlc,
    traditional_pivots,
)
from orb_intraday_bot.indicators.swings import SwingTracker
from orb_intraday_bot.indicators.zones import band_label, classify_zone
from tests.helpers import series

# H=100, L=80, C=90 gives clean numbers: P=90
PIV = traditional_pivots(100, 80, 90)


def test_traditional_pivot_values() -> None:
    assert PIV.p == 90
    assert (PIV.r1, PIV.s1) == (100, 80)
    assert (PIV.r2, PIV.s2) == (110, 70)
    assert (PIV.r3, PIV.s3) == (120, 60)
    assert (PIV.r4, PIV.s4) == (130, 50)
    assert (PIV.r5, PIV.s5) == (140, 40)


def test_formulas_match_pine_reference_forms() -> None:
    # The Pine script writes R3/R4 as H + k*(P-L) and S3/S4 as L - k*(H-P).
    h, low, c = 22650.0, 22480.0, 22610.0
    p = traditional_pivots(h, low, c)
    assert p.r3 == pytest.approx(h + 2 * (p.p - low))
    assert p.r4 == pytest.approx(h + 3 * (p.p - low))
    assert p.s3 == pytest.approx(low - 2 * (h - p.p))
    assert p.s4 == pytest.approx(low - 3 * (h - p.p))


def test_levels_are_ordered() -> None:
    values = [v for _, v in PIV.ordered()]
    assert values == sorted(values)
    assert [n for n, _ in PIV.ordered()][:3] == ["S5", "S4", "S3"]


def test_levels_between_and_nearest() -> None:
    assert [n for n, _ in PIV.levels_between(92, 125)] == ["R1", "R2", "R3"]
    assert [n for n, _ in PIV.levels_between(125, 92)] == ["R1", "R2", "R3"]
    assert PIV.levels_between(100, 110) == []   # strictly between, both ends excluded
    assert PIV.nearest_above(95) == ("R1", 100)
    assert PIV.nearest_below(95) == ("P", 90)
    assert PIV.nearest_above(1000) is None


def test_pivots_from_session_candles() -> None:
    candles = series([(90, 95, 85, 92), (92, 100, 80, 88), (88, 96, 84, 90)])
    assert session_ohlc(candles) == (100, 80, 90)
    assert pivots_from_session(candles) == PIV


def test_session_ohlc_requires_candles() -> None:
    with pytest.raises(ValueError):
        session_ohlc([])


@pytest.mark.parametrize(
    ("price", "zone"),
    [
        (100.01, Zone.BULLISH),
        (100.0, Zone.NEUTRAL),    # exactly R1 is not "above R1"
        (90.0, Zone.NEUTRAL),
        (80.0, Zone.NEUTRAL),     # exactly S1 is not "below S1"
        (79.99, Zone.BEARISH),
    ],
)
def test_zone_boundaries(price: float, zone: Zone) -> None:
    assert classify_zone(price, PIV) == zone


def test_band_labels() -> None:
    assert band_label(95, PIV) == "P-R1"
    assert band_label(105, PIV) == "R1-R2"
    assert band_label(75, PIV) == "S2-S1"
    assert band_label(500, PIV) == "above R5"
    assert band_label(1, PIV) == "below S5"


def test_swing_high_and_low_with_strength_one() -> None:
    #            h   l
    candles = series([
        (10, 11, 9, 10),      # 0
        (10, 13, 10, 12),     # 1  swing high 13
        (12, 12, 8, 9),       # 2  swing low 8
        (9, 11, 9, 10),       # 3
    ])
    tracker = SwingTracker(strength=1)
    found = [tracker.update(c) for c in candles]
    assert found[0] == [] and found[1] == []
    assert [s.kind for s in found[2]] == ["high"]     # confirmed one candle later
    assert found[2][0].price == 13
    assert [s.kind for s in found[3]] == ["low"]
    assert found[3][0].price == 8


def test_swings_confirmed_with_delay_equal_to_strength() -> None:
    candles = series([
        (10, 11, 9, 10), (10, 12, 10, 11), (11, 15, 11, 14),   # 15 is the peak (index 2)
        (14, 14, 12, 13), (13, 13, 11, 12),
    ])
    tracker = SwingTracker(strength=2)
    results = [tracker.update(c) for c in candles]
    assert all(r == [] for r in results[:4])
    assert [s.price for s in results[4] if s.kind == "high"] == [15]


def test_swing_strictness_equal_left_is_not_a_swing() -> None:
    candles = series([(10, 12, 9, 11), (11, 12, 10, 11), (11, 11, 10, 10)])   # equal highs
    tracker = SwingTracker(strength=1)
    for c in candles:
        tracker.update(c)
    assert [s for s in tracker.swings if s.kind == "high"] == []


def test_swing_last_helper() -> None:
    candles = series([
        (10, 11, 9, 10), (10, 13, 10, 12), (12, 12, 8, 9),
        (9, 14, 9, 13), (13, 13, 7, 8), (8, 9, 8, 9),
    ])
    tracker = SwingTracker(strength=1)
    for c in candles:
        tracker.update(c)
    assert [s.price for s in tracker.last("high", 2)] == [13, 14]
    assert tracker.last("low", 1)[0].price == 7


def test_swing_strength_must_be_positive() -> None:
    with pytest.raises(ValueError):
        SwingTracker(strength=0)
