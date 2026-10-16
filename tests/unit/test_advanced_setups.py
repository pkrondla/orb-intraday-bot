"""Phase 4 scenario tests: additional whipsaws, consolidation, 2LL/3LL/2HH/3HH, fake breaks,
trending-day label, and the combined SetupDetector."""

from __future__ import annotations

from datetime import datetime

import pytest

from orb_intraday_bot.config.settings import (
    EnabledSetups,
    StrategyConfig,
)
from orb_intraday_bot.core.models import Direction
from orb_intraday_bot.core.signals import (
    PatternEvent,
    PatternKind,
    Setup,
    SetupSignal,
    StepResult,
)
from orb_intraday_bot.indicators.opening_range import OpeningRangeTracker
from orb_intraday_bot.patterns.consolidation import ConsolidationMachine
from orb_intraday_bot.patterns.detector import SetupDetector
from orb_intraday_bot.patterns.fake_break import FakeBreakMachine
from orb_intraday_bot.patterns.structure import StructureWhipsawMachine
from orb_intraday_bot.patterns.trending import TrendingDayTracker
from orb_intraday_bot.patterns.whipsaw import RejectionWhipsawMachine
from tests.helpers import at, candle, series

Spec = tuple[float, float, float, float]


def feed(machine: object, specs: list[Spec]) -> list[StepResult]:
    out = []
    for c in series(specs):
        out.append(machine.update(c))  # type: ignore[attr-defined]
    return out


def sigs(results: list[StepResult]) -> list[SetupSignal]:
    return [s for r in results for s in r.signals]


def kinds(results: list[StepResult]) -> list[PatternKind]:
    return [e.kind for r in results for e in r.events]


# ---------------------------------------------------------------------------------------------
# Additional whipsaw
# ---------------------------------------------------------------------------------------------
def whipsaw(direction: Direction, stop_on: str, level: float = 110.0) -> RejectionWhipsawMachine:
    return RejectionWhipsawMachine(
        level_name="ORH" if direction is Direction.LONG else "ORL",
        level=level,
        direction=direction,
        parent="ORB" if stop_on == "close" else "TRAP",
        stop_on=stop_on,  # type: ignore[arg-type]
        target_ref="R2" if direction is Direction.LONG else "S2",
        min_rejections=2,
        tolerance=1.0,
        parent_candle=at(9, 30),
    )


LONG_WHIPSAW: list[Spec] = [
    (113, 114, 109.5, 111),      # 0 rejection #1 (low within 1 point of 110), high 114
    (112, 113, 112, 112.5),      # 1 away from the level
    (112.5, 113, 110.5, 112),    # 2 rejection #2, high 113
    (112, 113.6, 111.5, 113.4),  # 3 breaks 113 -> continuation entry
]


def test_orb_whipsaw_enters_on_break_of_last_rejection_high() -> None:
    res = feed(whipsaw(Direction.LONG, "close"), LONG_WHIPSAW)
    assert kinds(res).count(PatternKind.WHIPSAW_REJECTION) == 2
    (sig,) = sigs(res)
    assert sig.setup is Setup.ADDITIONAL_WHIPSAW
    assert sig.direction is Direction.LONG
    assert sig.entry_price == 113
    assert sig.stop_level == 110 and sig.stop_on == "close"
    assert sig.target_ref == "R2"
    assert sig.candle_start == at(9, 24)   # 4th candle of the series


def test_whipsaw_needs_two_rejections() -> None:
    specs = LONG_WHIPSAW[:1] + [(112, 115, 111.5, 114)]    # one rejection, then a breakout
    assert sigs(feed(whipsaw(Direction.LONG, "close"), specs)) == []


def test_consecutive_touching_candles_are_one_rejection() -> None:
    specs: list[Spec] = [
        (113, 114, 109.5, 111),
        (111, 112, 110.8, 111.5),    # still touching: same rejection
        (111.5, 115, 111.2, 114),    # breaks 112 but only one rejection counted
    ]
    assert sigs(feed(whipsaw(Direction.LONG, "close"), specs)) == []


def test_whipsaw_cancelled_by_close_beyond_the_stop() -> None:
    specs = LONG_WHIPSAW[:3] + [(112, 112.5, 108, 109), (109, 120, 108, 119)]
    res = feed(whipsaw(Direction.LONG, "close"), specs)
    assert PatternKind.WHIPSAW_CANCELLED in kinds(res)
    assert sigs(res) == []


def test_whipsaw_gap_fills_at_open() -> None:
    specs = LONG_WHIPSAW[:3] + [(114, 115, 113.5, 114.5)]
    (sig,) = sigs(feed(whipsaw(Direction.LONG, "close"), specs))
    assert sig.entry_price == 114
    assert "gap" in sig.entry_basis


SHORT_TRAP_WHIPSAW: list[Spec] = [
    (106, 109.5, 105, 106),      # 0 rejection #1: high within 1 point of ORH 110
    (105.5, 106.5, 104, 104.5),  # 1 away
    (105, 109.2, 104.5, 105),    # 2 rejection #2, low 104.5
    (104.8, 105, 104, 104.2),    # 3 breaks 104.5 -> continuation short
]


def test_trap_whipsaw_short_mirror_uses_touch_stop() -> None:
    res = feed(whipsaw(Direction.SHORT, "touch"), SHORT_TRAP_WHIPSAW)
    (sig,) = sigs(res)
    assert sig.direction is Direction.SHORT
    assert sig.entry_price == 104.5
    assert sig.stop_level == 110 and sig.stop_on == "touch"


def test_trap_whipsaw_cancelled_when_price_trades_through_the_level() -> None:
    specs = SHORT_TRAP_WHIPSAW[:3] + [(105, 110.5, 104, 105)]
    res = feed(whipsaw(Direction.SHORT, "touch"), specs)
    assert PatternKind.WHIPSAW_CANCELLED in kinds(res)


# ---------------------------------------------------------------------------------------------
# Consolidation breakout
# ---------------------------------------------------------------------------------------------
def cons(direction: Direction = Direction.LONG) -> ConsolidationMachine:
    level = 110.0 if direction is Direction.LONG else 95.0
    return ConsolidationMachine(
        level_name="ORH" if direction is Direction.LONG else "ORL",
        level=level, direction=direction,
        min_touches=3, min_minutes=45, max_minutes=60, tolerance=1.0,
    )


def run_cons(
    machine: ConsolidationMachine, items: list[tuple[str, float, float, float, float]]
) -> list[StepResult]:
    out = []
    for hhmm, o, h, low, c in items:
        hh, mm = (int(x) for x in hhmm.split(":"))
        out.append(machine.update(candle(at(hh, mm), o, h, low, c), 110.0, 95.0))
    return out


def touch(t: str) -> tuple[str, float, float, float, float]:
    return (t, 107, 109.5, 106, 108)          # reaches 109.5 (within 1 of ORH), closes inside


def away(t: str) -> tuple[str, float, float, float, float]:
    return (t, 104, 105, 103, 104)


def test_consolidation_enters_directly_at_the_breakout_close() -> None:
    items = [
        touch("10:00"), away("10:03"), touch("10:15"), away("10:18"),
        touch("10:30"), away("10:33"),
        ("10:45", 108, 112, 108, 111),        # closes above ORH after 45 minutes, 3 rejections
    ]
    res = run_cons(cons(), items)
    (sig,) = sigs(res)
    assert sig.setup is Setup.CONSOLIDATION_BREAKOUT
    assert sig.direction is Direction.LONG
    assert sig.entry_price == 111
    assert sig.stop_on == "none"
    assert sig.target_ref == "OR_PROJECTION_OR_PIVOT"
    assert sig.target_price == 125          # ORH 110 + OR range (110 - 95)
    assert sig.sign_id == "2026-10-15|ORH|10:45"   # same as the ORB sign on that candle


def test_consolidation_needs_45_minutes() -> None:
    items = [touch("10:00"), touch("10:15"), touch("10:30"), ("10:42", 108, 112, 108, 111)]
    assert sigs(run_cons(cons(), items)) == []


def test_consolidation_needs_min_touches() -> None:
    items = [touch("10:00"), away("10:03"), touch("10:30"), ("10:48", 108, 112, 108, 111)]
    assert sigs(run_cons(cons(), items)) == []


def test_consecutive_touching_candles_count_once() -> None:
    items = [
        touch("10:00"), touch("10:03"), touch("10:06"),     # one episode
        away("10:09"), touch("10:30"),                       # two episodes so far
        ("10:48", 108, 112, 108, 111),
    ]
    assert sigs(run_cons(cons(), items)) == []


def test_old_rejections_are_forgotten() -> None:
    # touches at 09:00 (older than 60 min at 10:48), 10:00, 10:15, 10:30 -> 3 recent ones
    items = [
        touch("09:00"), away("09:03"), touch("10:00"), away("10:03"),
        touch("10:15"), away("10:18"), touch("10:30"), away("10:33"),
        ("10:48", 108, 112, 108, 111),
    ]
    assert len(sigs(run_cons(cons(), items))) == 1
    items2 = [
        touch("09:00"), away("09:03"), touch("10:30"), away("10:33"),
        touch("10:40"), away("10:43"), ("10:48", 108, 112, 108, 111),
    ]
    assert sigs(run_cons(cons(), items2)) == []


def test_close_beyond_clears_the_rejections() -> None:
    items = [
        touch("10:00"), away("10:03"), touch("10:15"), away("10:18"), touch("10:30"),
        ("10:33", 108, 112, 108, 111),            # breakout too early: no signal, clears
        away("10:36"),
        ("10:48", 108, 112, 108, 111),            # a second close beyond with nothing recorded
    ]
    assert sigs(run_cons(cons(), items)) == []


def test_consolidation_short_mirror_at_orl() -> None:
    def low_touch(t: str) -> tuple[str, float, float, float, float]:
        return (t, 98, 99, 95.5, 97)               # reaches 95.5, closes inside

    def far(t: str) -> tuple[str, float, float, float, float]:
        return (t, 100, 102, 99, 101)

    items = [
        low_touch("10:00"), far("10:03"), low_touch("10:15"), far("10:18"),
        low_touch("10:30"), far("10:33"), ("10:45", 97, 97, 93, 94),
    ]
    (sig,) = sigs(run_cons(cons(Direction.SHORT), items))
    assert sig.direction is Direction.SHORT and sig.entry_price == 94
    assert sig.target_price == 80                  # ORL 95 - range 15


# ---------------------------------------------------------------------------------------------
# 2LL / 3LL and 2HH / 3HH
# ---------------------------------------------------------------------------------------------
def mirror(specs: list[Spec]) -> list[Spec]:
    """Reflect prices around 100 so a rise becomes a fall (p -> 200 - p)."""
    return [(200 - o, 200 - lo, 200 - h, 200 - c) for o, h, lo, c in specs]


RISE_2LL: list[Spec] = [
    (100, 105, 95, 104),        # 0 start of the day, low 95
    (104, 115, 103, 114),       # 1 new high
    (114, 118, 112, 117),       # 2 new high 118 (the peak)
    (117, 117.5, 110, 111),     # 3 pullback: low 110 < 112   -> 1st lower low
    (111, 112, 108, 109),       # 4 low 108 < 110             -> 2nd lower low
    (109, 113, 109, 112),       # 5 inside, no new low
    (112, 119, 111, 118),       # 6 breaks 118: continuation
]


def long_structure() -> StructureWhipsawMachine:
    return StructureWhipsawMachine(direction=Direction.LONG)


def short_structure() -> StructureWhipsawMachine:
    return StructureWhipsawMachine(direction=Direction.SHORT)


def test_2ll_continuation_long() -> None:
    (sig,) = sigs(feed(long_structure(), RISE_2LL))
    assert sig.setup is Setup.STRUCTURE_WHIPSAW
    assert sig.level_name == "2LL"
    assert sig.direction is Direction.LONG
    assert sig.entry_price == 118            # break of the pullback high
    assert sig.stop_level == 108 and sig.stop_on == "touch"
    assert sig.target_ref == "R2"
    assert sig.candle_start == at(9, 33)


def test_3ll_continuation_long() -> None:
    specs = list(RISE_2LL)
    specs[5] = (109, 110, 106, 107)           # third lower low (106 < 108)
    specs.insert(6, (107, 110, 107, 109))
    (sig,) = sigs(feed(long_structure(), specs))
    assert sig.level_name == "3LL"
    assert sig.stop_level == 106


def test_single_lower_low_is_a_normal_pullback() -> None:
    specs = list(RISE_2LL)
    specs[4] = (111, 112, 110.5, 111.5)       # no second lower low ...
    specs[5] = (111.5, 113, 110.5, 112)       # ... and none afterwards either
    assert sigs(feed(long_structure(), specs)) == []


def test_four_lower_lows_is_not_a_whipsaw() -> None:
    specs = list(RISE_2LL)
    specs[5] = (109, 109.5, 106, 107)
    specs.insert(6, (107, 108, 104, 105))     # 4th lower low
    specs[7] = (105, 119, 104.5, 118)
    assert sigs(feed(long_structure(), specs)) == []


def test_pullback_below_the_rise_start_breaks_the_structure() -> None:
    specs = list(RISE_2LL)
    specs[4] = (111, 112, 94, 96)             # undercuts the day's low 95
    assert sigs(feed(long_structure(), specs)) == []


def test_small_rise_is_ignored() -> None:
    specs = [(o, h, lo + 4, c) if i == 0 else (o, h, lo, c)
             for i, (o, h, lo, c) in enumerate(RISE_2LL)]    # day low 99: rise only 19 points
    assert sigs(feed(long_structure(), specs)) == []


def test_2hh_continuation_short_is_the_mirror() -> None:
    (sig,) = sigs(feed(short_structure(), mirror(RISE_2LL)))
    assert sig.level_name == "2HH"
    assert sig.direction is Direction.SHORT
    assert sig.entry_price == 82             # 200 - 118
    assert sig.stop_level == 92              # 200 - 108
    assert sig.target_ref == "S2"


def test_structure_signal_not_produced_in_the_wrong_direction() -> None:
    assert sigs(feed(short_structure(), RISE_2LL)) == []
    assert sigs(feed(long_structure(), mirror(RISE_2LL))) == []


# ---------------------------------------------------------------------------------------------
# Fake breakdown (long) and fake breakout (short)
# ---------------------------------------------------------------------------------------------
def fake(direction: Direction = Direction.LONG) -> FakeBreakMachine:
    return FakeBreakMachine(
        direction=direction, consolidation_candles=6, max_range_points=30, edge_touches=2,
        max_fake_candles=3, min_reversal_leg_points=15, max_wait_candles=10, retracement=0.5,
    )


RANGE: list[Spec] = [
    (110, 120, 108, 112),    # top touch #1
    (112, 115, 100, 104),    # low touch #1
    (104, 114, 102, 110),
    (110, 120, 109, 118),    # top touch #2
    (118, 119, 100, 105),    # low touch #2
    (105, 112, 101, 108),
]
FAKE_BREAK: list[Spec] = [(108, 109, 95, 101)]      # trades to 95, closes back above 100
REVERSAL: list[Spec] = [
    (101, 115, 100, 114),    # leg 109 -> not sharp enough yet (14), high 115
    (114, 118, 112, 116),    # leg 118-95 = 23; 50% retracement = 106.5; low 112: not yet
    (116, 117, 103, 106),    # low 103 <= 106.5 -> entry
]


def test_fake_breakdown_enters_at_half_the_reversal_leg() -> None:
    res = feed(fake(), RANGE + FAKE_BREAK + REVERSAL)
    (sig,) = sigs(res)
    assert sig.setup is Setup.FAKE_BREAK
    assert sig.direction is Direction.LONG
    assert sig.entry_price == 106.5
    assert sig.stop_level == 95 and sig.stop_on == "touch"
    assert sig.target_ref == "RANGE_OPPOSITE_END" and sig.target_price == 120
    assert PatternKind.FAKE_BREAK in kinds(res) and PatternKind.FAKE_REVERSAL in kinds(res)


def test_fake_breakout_short_is_the_mirror() -> None:
    res = feed(fake(Direction.SHORT), mirror(RANGE + FAKE_BREAK + REVERSAL))
    (sig,) = sigs(res)
    assert sig.direction is Direction.SHORT
    assert sig.entry_price == 93.5           # 200 - 106.5
    assert sig.stop_level == 105             # 200 - 95
    assert sig.target_price == 80            # 200 - 120


def test_real_break_is_not_a_fake() -> None:
    real = [(105, 106, 95, 98), (98, 99, 94, 97), (97, 98, 93, 97), (97, 98, 92, 97)]
    res = feed(fake(), RANGE + real)
    assert PatternKind.FAKE_CANCELLED in kinds(res)
    assert sigs(res) == []


def test_trading_below_the_fake_extreme_cancels() -> None:
    res = feed(fake(), RANGE + FAKE_BREAK + [(101, 104, 94, 100.5)])
    assert PatternKind.FAKE_CANCELLED in kinds(res)


def test_reaching_the_range_end_without_a_retracement_cancels() -> None:
    res = feed(fake(), RANGE + FAKE_BREAK + [(101, 121, 100, 120)])
    assert PatternKind.FAKE_CANCELLED in kinds(res)
    assert sigs(res) == []


def test_no_range_when_an_edge_is_touched_only_once() -> None:
    weak = list(RANGE)
    weak[3] = (110, 116, 109, 115)           # second top touch removed (needs >= 117)
    weak[4] = (115, 116.5, 100, 105)
    res = feed(fake(), weak + FAKE_BREAK + REVERSAL)
    assert PatternKind.FAKE_BREAK not in kinds(res)


def test_no_range_when_too_wide() -> None:
    wide = list(RANGE)
    wide[1] = (112, 115, 85, 90)             # range 85..120 = 35 > 30
    res = feed(fake(), wide + FAKE_BREAK + REVERSAL)
    assert PatternKind.FAKE_BREAK not in kinds(res)


# ---------------------------------------------------------------------------------------------
# Trending day
# ---------------------------------------------------------------------------------------------
def ev(kind: PatternKind, level: str, direction: Direction) -> PatternEvent:
    return PatternEvent(kind, level, direction, at(10, 0))


DUMMY = candle(at(10, 0), 100, 101, 99, 100)


def test_uptrend_bear_trap_plus_orh_breakout() -> None:
    t = TrendingDayTracker()
    assert t.update(DUMMY, [ev(PatternKind.FAILED_CASE_1, "ORL", Direction.SHORT)], []) == []
    out = t.update(DUMMY, [ev(PatternKind.CONFIRMED, "ORH", Direction.LONG)], [])
    assert [e.kind for e in out] == [PatternKind.TREND_DAY]
    assert "UPTREND" in out[0].detail and t.state == "UPTREND"


def test_downtrend_bull_trap_plus_orl_breakdown() -> None:
    t = TrendingDayTracker()
    t.update(DUMMY, [ev(PatternKind.FAILED_CASE_2, "ORH", Direction.LONG)], [])
    out = t.update(DUMMY, [ev(PatternKind.CONFIRMED, "ORL", Direction.SHORT)], [])
    assert t.state == "DOWNTREND" and "DOWNTREND" in out[0].detail


def test_failed_bull_trap_plus_orh_breakout() -> None:
    t = TrendingDayTracker()
    t.update(DUMMY, [ev(PatternKind.FAILED_CASE_1, "ORH", Direction.LONG)], [])   # bull trap
    t.update(DUMMY, [ev(PatternKind.SIGN, "ORH", Direction.LONG)], [])   # closes above again
    assert t.state == "none"
    t.update(DUMMY, [ev(PatternKind.CONFIRMED, "ORH", Direction.LONG)], [])
    assert t.state == "UPTREND"
    assert "FAILED_BULL_TRAP" in t.labels


def test_a_first_sign_is_not_a_failed_trap() -> None:
    t = TrapFree = TrendingDayTracker()
    t.update(DUMMY, [ev(PatternKind.SIGN, "ORH", Direction.LONG),
                     ev(PatternKind.CONFIRMED, "ORH", Direction.LONG)], [])
    assert t.state == "none" and TrapFree is t


def test_trend_label_fires_once_and_both_directions_are_reported() -> None:
    t = TrendingDayTracker()
    first = t.update(DUMMY, [ev(PatternKind.FAILED_CASE_1, "ORL", Direction.SHORT),
                             ev(PatternKind.CONFIRMED, "ORH", Direction.LONG)], [])
    assert len(first) == 1
    again = t.update(DUMMY, [ev(PatternKind.CONFIRMED, "ORH", Direction.LONG)], [])
    assert again == []
    t.update(DUMMY, [ev(PatternKind.FAILED_CASE_1, "ORH", Direction.LONG),
                     ev(PatternKind.CONFIRMED, "ORL", Direction.SHORT)], [])
    assert t.state == "UP and DOWN (choppy)"


def test_consolidation_breakout_counts_as_the_breakout_label() -> None:
    t = TrendingDayTracker()
    sig = SetupSignal(
        setup=Setup.CONSOLIDATION_BREAKOUT, direction=Direction.LONG, level_name="ORH",
        level_value=110, candle_start=at(10, 0), entry_price=111, entry_basis="x",
        sign_id="s", stop_level=None, stop_on="none", target_ref=None,
    )
    t.update(DUMMY, [ev(PatternKind.FAILED_CASE_1, "ORL", Direction.SHORT)], [sig])
    assert t.state == "UPTREND"


# ---------------------------------------------------------------------------------------------
# Combined detector
# ---------------------------------------------------------------------------------------------
BASE: list[Spec] = [(100, 110, 95, 105), (105, 108, 98, 100)]       # ORH 110, ORL 95
ORB_LONG: list[Spec] = [
    (100, 112, 100, 111),       # sign
    (111, 112, 109, 110.5),     # retest
    (110.5, 113, 110, 112.5),   # confirmation (ORB signal)
]
AFTER_ORB: list[Spec] = [
    (112.5, 113.5, 109.8, 111),  # rejection #1
    (112, 112.5, 111.5, 112),    # away
    (112, 113, 110.5, 112.5),    # rejection #2, high 113
    (112.5, 113.2, 112, 113),    # breaks 113 -> additional whipsaw
]


def run_detector(
    specs: list[Spec], config: StrategyConfig | None = None
) -> list[StepResult]:
    cfg = config or StrategyConfig()
    tracker = OpeningRangeTracker()
    detector = SetupDetector(cfg)
    out = []
    for c in series(BASE + specs):
        tracker.update(c)
        out.append(detector.update(c, tracker.orh, tracker.orl))
    return out[2:]


def test_orb_followed_by_additional_whipsaw() -> None:
    res = run_detector(ORB_LONG + AFTER_ORB)
    by_setup = {s.setup: s for s in sigs(res)}
    assert Setup.ORB_BREAKOUT in by_setup
    ws = by_setup[Setup.ADDITIONAL_WHIPSAW]
    assert ws.direction is Direction.LONG and ws.entry_price == 113
    assert ws.candle_start > by_setup[Setup.ORB_BREAKOUT].candle_start
    assert ws.sign_id != by_setup[Setup.ORB_BREAKOUT].sign_id


def test_whipsaw_tracking_starts_after_the_confirmation_candle() -> None:
    # the confirmation candle itself touches the level (low 110) but is not a rejection
    res = run_detector(ORB_LONG)
    assert PatternKind.WHIPSAW_REJECTION not in kinds(res)


def test_whipsaw_flag_off_suppresses_the_signal() -> None:
    cfg = StrategyConfig(enabled_setups=EnabledSetups(additional_whipsaw=False))
    res = run_detector(ORB_LONG + AFTER_ORB, cfg)
    assert Setup.ADDITIONAL_WHIPSAW not in {s.setup for s in sigs(res)}
    assert Setup.ORB_BREAKOUT in {s.setup for s in sigs(res)}


def test_both_sides_trap_must_stay_off() -> None:
    cfg = StrategyConfig(enabled_setups=EnabledSetups(both_sides_trap=True))
    with pytest.raises(NotImplementedError):
        SetupDetector(cfg)


def test_detector_is_per_day() -> None:
    detector = SetupDetector(StrategyConfig())
    detector.update(candle(at(9, 15), 1, 2, 0.5, 1.5), None, None)
    with pytest.raises(ValueError):
        detector.update(
            candle(datetime(2026, 10, 16, 9, 15, tzinfo=at(9, 15).tzinfo), 1, 2, 0.5, 1.5),
            None,
            None,
        )


def test_trending_label_comes_through_the_detector() -> None:
    # bear trap on ORL (Case 1), then ORH breakout confirmed -> uptrend
    specs: list[Spec] = [
        (96, 97, 93, 94),            # sign below ORL 95
        (94, 95.6, 93.8, 95.5),      # closes back above ORL: bear trap (Case 1)
        (95.5, 112, 95.5, 111),      # closes above ORH 110: sign
        (111, 112, 109, 110.5),      # retest
        (110.5, 113, 110, 112.5),    # confirmation
    ]
    res = run_detector(specs)
    trend = [e for r in res for e in r.events if e.kind is PatternKind.TREND_DAY]
    assert len(trend) == 1 and "UPTREND" in trend[0].detail
