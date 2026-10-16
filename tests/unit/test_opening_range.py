from __future__ import annotations

from datetime import date

from orb_intraday_bot.core.events import LevelSet
from orb_intraday_bot.core.models import Candle
from orb_intraday_bot.indicators.opening_range import OpeningRangeTracker
from tests.helpers import at, series


def run(candles: list[Candle]) -> tuple[OpeningRangeTracker, list[LevelSet]]:
    tracker = OpeningRangeTracker()
    events: list[LevelSet] = []
    for c in candles:
        events.extend(tracker.update(c))
    return tracker, events


def hl(highs: list[float], lows: list[float]) -> list[Candle]:
    """Candles with the given highs and lows (open/close placed inside the range)."""
    specs = [((h + low) / 2, h, low, (h + low) / 2) for h, low in zip(highs, lows, strict=True)]
    return series(specs)


def test_spec_example_orh() -> None:
    # highs 24,520 / 24,540 / 24,560 / 24,550 -> run is the first three, ORH = 24,560
    tracker, events = run(hl([24520, 24540, 24560, 24550], [24500, 24510, 24520, 24530]))
    assert tracker.orh == 24560
    orh_events = [e for e in events if e.kind == "ORH"]
    assert len(orh_events) == 1
    assert orh_events[0].candle_start == at(9, 24)   # set at the close of the 4th candle


def test_spec_example_equal_high_extends_run() -> None:
    # highs 24,520 / 24,540 / 24,540 / 24,530 -> the equal high extends, ORH = 24,540
    tracker, _ = run(hl([24520, 24540, 24540, 24530], [24500, 24510, 24520, 24530]))
    assert tracker.orh == 24540


def test_first_candle_alone_is_the_run() -> None:
    tracker, _ = run(hl([24520, 24510], [24500, 24505]))
    assert tracker.orh == 24520


def test_orl_mirrors_with_lower_or_equal_lows() -> None:
    # lows 24,500 / 24,480 / 24,480 / 24,490 -> equal low extends, ORL = 24,480
    tracker, _ = run(hl([24560, 24550, 24540, 24545], [24500, 24480, 24480, 24490]))
    assert tracker.orl == 24480


def test_levels_are_independent() -> None:
    # ORH run ends at candle 2; ORL keeps falling for 5 candles
    highs = [24520, 24510, 24500, 24490, 24480, 24470]
    lows = [24500, 24490, 24480, 24470, 24460, 24465]
    tracker, events = run(hl(highs, lows))
    assert tracker.orh == 24520
    assert tracker.orl == 24460
    by_kind = {e.kind: e.candle_start for e in events}
    assert by_kind["ORH"] == at(9, 18)
    assert by_kind["ORL"] == at(9, 30)


def test_run_that_never_ends_leaves_level_unset() -> None:
    tracker, events = run(hl([100, 101, 102, 103], [90, 91, 92, 93]))
    assert tracker.orh is None
    assert not [e for e in events if e.kind == "ORH"]


def test_level_is_set_once_and_never_changes() -> None:
    tracker, events = run(hl([100, 99, 105, 110, 120], [90, 91, 92, 93, 94]))
    assert tracker.orh == 100
    assert len([e for e in events if e.kind == "ORH"]) == 1


def test_resets_on_new_day() -> None:
    day1 = hl([100, 99], [90, 91])
    day2 = series([(200, 210, 195, 205), (205, 208, 200, 202)], first=at(9, 15, date(2026, 10, 16)))
    tracker = OpeningRangeTracker()
    for c in day1:
        tracker.update(c)
    assert tracker.orh == 100
    tracker.update(day2[0])
    assert tracker.orh is None   # reset
    tracker.update(day2[1])
    assert tracker.orh == 210


def test_is_complete() -> None:
    tracker, _ = run(hl([100, 99, 98], [90, 91, 92]))
    assert tracker.is_complete
