"""Retest tolerance, swing-high confirmation level, and the 15 Oct 2026 regression day.

Levels in the synthetic scenarios: ORH = 110, ORL = 95 (see test_core_setups.py).
"""

from __future__ import annotations

from datetime import datetime

from orb_intraday_bot.config.settings import EnabledSetups, EntriesConfig
from orb_intraday_bot.core.clock import IST
from orb_intraday_bot.core.models import Candle, Direction
from orb_intraday_bot.core.signals import PatternKind, Setup, StepResult
from orb_intraday_bot.indicators.opening_range import OpeningRangeTracker
from orb_intraday_bot.patterns.core_setups import CoreSetupDetector
from tests.helpers import at, series
from tests.unit.test_core_setups import BASE, Spec, kinds, signals


def run(specs: list[Spec], tolerance: float = 1.0) -> list[StepResult]:
    tracker = OpeningRangeTracker()
    detector = CoreSetupDetector(
        EnabledSetups(), None, EntriesConfig(retest_tolerance_points=tolerance)
    )
    results = []
    for candle in series(BASE + specs):
        tracker.update(candle)
        results.append(detector.update(candle, tracker.orh, tracker.orl))
    return results[2:]


# ---- retest tolerance ----------------------------------------------------------------------
def retest_scenario(retest_low: float) -> list[Spec]:
    return [
        (112, 116, 111.5, 115),                    # sign (close 115 > 110)
        (115, 115.5, retest_low, 112),             # candidate retest candle
        (112, 117, 111.8, 116),                    # would confirm (high above 116)
    ]


def test_low_within_tolerance_is_a_retest() -> None:
    res = run(retest_scenario(110.8), tolerance=1.0)       # 0.8 above ORH
    assert PatternKind.RETEST in kinds(res[1])
    assert len(signals(res, Setup.ORB_BREAKOUT)) == 1


def test_low_exactly_at_tolerance_is_a_retest() -> None:
    res = run(retest_scenario(111.0), tolerance=1.0)
    assert PatternKind.RETEST in kinds(res[1])


def test_low_beyond_tolerance_is_not_a_retest() -> None:
    res = run(retest_scenario(111.1), tolerance=1.0)
    assert PatternKind.RETEST not in kinds(res[1])
    assert signals(res) == []


def test_zero_tolerance_requires_an_exact_touch() -> None:
    assert signals(run(retest_scenario(110.1), tolerance=0.0)) == []
    assert len(signals(run(retest_scenario(110.0), tolerance=0.0))) == 1


def test_short_side_tolerance_mirrors() -> None:
    specs: list[Spec] = [
        (94, 94.5, 91, 92),               # sign: close 92 < ORL 95
        (92, 94.2, 91.5, 93),             # high 94.2 is 0.8 below ORL: retest within 1 point
        (93, 93.5, 90, 91),               # breaks the low 91 -> confirmation
    ]
    res = run(specs, tolerance=1.0)
    assert PatternKind.RETEST in kinds(res[1])
    (sig,) = signals(res, Setup.ORB_BREAKOUT)
    assert sig.direction is Direction.SHORT


def test_failure_uses_plain_close_without_tolerance() -> None:
    # close 109.5 is within 1 point of ORH but still a failure
    specs: list[Spec] = [(112, 116, 111.5, 115), (115, 115.5, 109, 109.5)]
    res = run(specs, tolerance=1.0)
    assert PatternKind.FAILED_CASE_1 in kinds(res[1])


# ---- swing high = most recent swing before the retest pullback -------------------------------
SWING_SCENARIO: list[Spec] = [
    (108, 125, 108, 123),    # 0 sign: spike, high 125
    (118, 119, 115, 116),    # 1 pullback, high 119
    (116, 121, 115.5, 120),  # 2 bounce, high 121: the most recent swing high
    (115, 116, 110.5, 111),  # 3 retest (low within 1 point of 110), high 116 below 121
    (111, 121.5, 111, 121),  # 4 breaks 121 (but is far below the sign candle's 125)
]


def test_confirmation_level_is_the_most_recent_swing_high_not_the_highest() -> None:
    res = run(SWING_SCENARIO)
    (sig,) = signals(res, Setup.ORB_BREAKOUT)
    assert sig.candle_start == at(9, 33)          # index 4 -> 09:21 + 4*3 min
    assert "swing 121" in sig.note
    # previous candle low 110.5 vs the confirmation candle low 111 -> not traded
    assert sig.entry_price == 121


def test_without_a_pullback_in_highs_the_leg_extreme_is_the_swing() -> None:
    specs: list[Spec] = [
        (111, 112, 110, 111.5),      # sign, high 112
        (111.5, 113, 110.5, 112),    # retest candle is the highest high: no earlier swing exists
        (112, 113.5, 111.5, 113),    # high above 113 -> confirms
    ]
    res = run(specs)
    assert len(signals(res, Setup.ORB_BREAKOUT)) == 1
    assert "swing 113" in signals(res)[0].note


def test_short_side_uses_most_recent_swing_low() -> None:
    specs: list[Spec] = [
        (96, 97, 80, 82),        # 0 sign: close 82 < ORL 95, spike low 80
        (84, 86, 83, 85),        # 1 bounce, low 83
        (85, 85.5, 81, 82),      # 2 drop, low 81: most recent swing low
        (86, 94.2, 85, 93),      # 3 retest: high 94.2 within 1 point of ORL
        (93, 93.5, 80.8, 81),    # 4 low 80.8 breaks 81 (but not the sign candle's 80)
    ]
    (sig,) = signals(run(specs), Setup.ORB_BREAKOUT)
    assert sig.direction is Direction.SHORT
    assert "swing 81" in sig.note


# ---- 15 Oct 2026 regression (real Groww candles, futures NIFTY26OCTFUT) ----------------------
REAL_0915_TO_1015 = [
    ("09:15", 22671.0, 22759.9, 22671.0, 22744.2),
    ("09:18", 22743.0, 22751.9, 22711.0, 22711.7),
    ("09:21", 22712.0, 22738.0, 22699.0, 22733.5),
    ("09:24", 22737.0, 22764.0, 22722.1, 22755.0),
    ("09:27", 22755.6, 22773.0, 22744.3, 22773.0),
    ("09:30", 22779.0, 22780.0, 22760.0, 22774.0),
    ("09:33", 22767.6, 22795.0, 22767.6, 22776.1),
    ("09:36", 22776.0, 22808.0, 22775.0, 22795.0),
    ("09:39", 22797.0, 22800.0, 22770.0, 22775.0),
    ("09:42", 22776.0, 22788.0, 22768.0, 22772.1),
    ("09:45", 22771.0, 22824.0, 22765.0, 22818.0),
    ("09:48", 22812.0, 22818.0, 22795.0, 22804.8),
    ("09:51", 22802.0, 22846.0, 22802.0, 22845.0),
    ("09:54", 22847.0, 22865.0, 22841.1, 22853.1),
    ("09:57", 22855.0, 22875.0, 22852.2, 22872.0),
    ("10:00", 22873.4, 22895.0, 22873.4, 22888.9),
]


def real_day() -> list[Candle]:
    out = []
    for hhmm, o, h, low, c in REAL_0915_TO_1015:
        hh, mm = (int(x) for x in hhmm.split(":"))
        start = datetime(2026, 10, 15, hh, mm, tzinfo=IST)
        out.append(Candle(start=start, open=o, high=h, low=low, close=c))
    return out


def test_15_oct_orh_and_orl() -> None:
    tracker = OpeningRangeTracker()
    for c in real_day():
        tracker.update(c)
    assert tracker.orh == 22759.9
    assert tracker.orl == 22671.0


def test_15_oct_orb_long_confirms_at_0933() -> None:
    tracker = OpeningRangeTracker()
    detector = CoreSetupDetector(EnabledSetups(), None, EntriesConfig())
    by_time: dict[str, StepResult] = {}
    for c in real_day():
        tracker.update(c)
        by_time[c.start.strftime("%H:%M")] = detector.update(c, tracker.orh, tracker.orl)

    assert kinds(by_time["09:27"]) == [PatternKind.SIGN]           # first close above ORH
    assert kinds(by_time["09:30"]) == [PatternKind.RETEST]         # low 22760.0, 0.1 above ORH
    assert PatternKind.CONFIRMED in kinds(by_time["09:33"])        # high 22795 > swing 22780

    all_signals = [s for r in by_time.values() for s in r.signals]
    orb = [s for s in all_signals if s.setup is Setup.ORB_BREAKOUT]
    assert len(orb) == 1                                           # 09:45 is a continuation
    sig = orb[0]
    assert sig.candle_start == at(9, 33)
    assert sig.direction is Direction.LONG and sig.level_name == "ORH"
    assert sig.entry_price == 22776.1                              # reference 22760.0 not traded
    assert "close" in sig.entry_basis
    assert "swing 22780" in sig.note
    assert sig.stop_level == 22759.9 and sig.stop_on == "close"


def test_15_oct_without_tolerance_the_retest_is_missed() -> None:
    """Documents why the tolerance exists: 22760.0 vs ORH 22759.9."""
    tracker = OpeningRangeTracker()
    detector = CoreSetupDetector(
        EnabledSetups(), None, EntriesConfig(retest_tolerance_points=0.0)
    )
    found = []
    for c in real_day():
        tracker.update(c)
        found.extend(detector.update(c, tracker.orh, tracker.orl).signals)
    assert found == []
