"""Scenario tests for Phase 3 (ORB, traps, Case 1/2, R2/S2 as OR level).

Every scenario starts with two candles that fix ORH = 110 and ORL = 95:
    09:15  O100 H110 L95 C105    09:18  O105 H108 L98 C100  (high and low both turn down)
Scenario candles follow from 09:21 and are indexed from 0 in the spec lists below.
"""

from __future__ import annotations

from datetime import date

import pytest

from orb_intraday_bot.config.settings import EnabledSetups
from orb_intraday_bot.core.models import Direction
from orb_intraday_bot.core.signals import (
    PatternKind,
    Setup,
    SetupSignal,
    StepResult,
)
from orb_intraday_bot.indicators.opening_range import OpeningRangeTracker
from orb_intraday_bot.indicators.pivots import Pivots, traditional_pivots
from orb_intraday_bot.patterns.core_setups import CoreSetupDetector
from tests.helpers import at, series

BASE = [(100, 110, 95, 105), (105, 108, 98, 100)]
Spec = tuple[float, float, float, float]


def run(
    specs: list[Spec],
    pivots: Pivots | None = None,
    enabled: EnabledSetups | None = None,
) -> list[StepResult]:
    """Results for the scenario candles only (the two OR candles are dropped)."""
    tracker = OpeningRangeTracker()
    detector = CoreSetupDetector(enabled or EnabledSetups(), pivots)
    results = []
    for candle in series(BASE + specs):
        tracker.update(candle)
        results.append(detector.update(candle, tracker.orh, tracker.orl))
    return results[2:]


def signals(results: list[StepResult], setup: Setup | None = None) -> list[SetupSignal]:
    return [s for r in results for s in r.signals if setup is None or s.setup is setup]


def kinds(result: StepResult) -> list[PatternKind]:
    return [e.kind for e in result.events]


# ---- standard ORB long ---------------------------------------------------------------------
LONG_ORB = [
    (100, 112, 100, 111),       # 0 sign: close 111 > ORH 110, peak 112
    (111, 112, 109, 110.5),     # 1 retest: low 109 <= 110, close still above
    (110.5, 113, 110, 112.5),   # 2 confirmation: high 113 > 112
]


def test_orb_long_full_sequence_fills_at_confirmation_close_when_reference_not_traded() -> None:
    res = run(LONG_ORB)
    assert kinds(res[0]) == [PatternKind.SIGN]
    assert kinds(res[1]) == [PatternKind.RETEST]
    assert PatternKind.CONFIRMED in kinds(res[2])
    (sig,) = signals(res)
    assert sig.setup is Setup.ORB_BREAKOUT
    assert sig.direction is Direction.LONG
    assert sig.level_name == "ORH" and sig.level_value == 110
    # previous candle low is 109 but the confirmation candle low was 110 -> never traded there
    assert sig.entry_price == 112.5
    assert "close" in sig.entry_basis
    assert sig.stop_level == 110 and sig.stop_on == "close"
    assert sig.target_ref == "R2"
    assert sig.candle_start == at(9, 27)
    assert not sig.aggressive


def test_orb_long_fills_at_previous_candle_low_when_traded() -> None:
    specs = list(LONG_ORB)
    specs[2] = (110.5, 113, 108.5, 112.5)   # dips to 108.5, through the reference 109
    (sig,) = signals(run(specs))
    assert sig.entry_price == 109
    assert "touched" in sig.entry_basis


def test_orb_long_needs_a_retest() -> None:
    specs = [
        (100, 112, 100, 111),    # sign
        (112.5, 114, 112, 113),  # continuation, no retest (low stays 2 points above ORH)
        (113, 116, 112, 115),    # breaks the peak, but there was no retest
    ]
    assert signals(run(specs)) == []


def test_orb_long_close_equal_to_level_is_not_a_failure() -> None:
    specs = [
        (100, 112, 100, 111),    # sign
        (111, 112, 109, 110),    # retest, closes exactly on ORH: allowed
        (110, 113, 110, 112),    # confirmation
    ]
    assert len(signals(run(specs), Setup.ORB_BREAKOUT)) == 1


def test_retest_and_confirmation_are_never_the_same_candle() -> None:
    specs = [
        (100, 112, 100, 111),    # sign, peak 112
        (111, 114, 109, 113),    # touches ORH AND breaks the peak: counts as the retest only
    ]
    res = run(specs)
    assert kinds(res[1]) == [PatternKind.RETEST]
    assert signals(res) == []


def test_orb_does_not_trade_on_the_candle_that_fixed_the_level() -> None:
    # Third candle makes ORH and ORL both ... 09:18 already fixed them. A candle that closes
    # above 110 on the same candle that fixed the level cannot exist (its high < ORH), so check
    # the machine has no state before the level exists.
    tracker = OpeningRangeTracker()
    detector = CoreSetupDetector(EnabledSetups())
    first, second = series(BASE)
    tracker.update(first)
    res = detector.update(first, tracker.orh, tracker.orl)
    assert res.events == [] and detector.breakouts == {}
    tracker.update(second)
    detector.update(second, tracker.orh, tracker.orl)
    assert set(detector.breakouts) == {"ORH", "ORL"}


def test_stale_sign_after_failure_starts_a_new_cycle() -> None:
    specs = [
        (100, 112, 100, 111),    # sign
        (111, 111, 108, 109),    # fails (Case 1)
        (109, 113, 109, 112),    # closes above again: new sign
    ]
    res = run(specs)
    assert PatternKind.FAILED_CASE_1 in kinds(res[1])
    assert PatternKind.SIGN in kinds(res[2])


# ---- standard ORB short (mirror) -----------------------------------------------------------
SHORT_ORB = [
    (96, 97, 93, 94),        # 0 sign: close 94 < ORL 95, peak (low) 93
    (94, 95.5, 93.5, 94.5),  # 1 retest: high 95.5 >= 95, close 94.5 still below
    (94.5, 94.8, 92, 92.5),  # 2 confirmation: low 92 < 93
]


def test_orb_short_full_sequence() -> None:
    res = run(SHORT_ORB)
    assert kinds(res[0]) == [PatternKind.SIGN]
    assert kinds(res[1]) == [PatternKind.RETEST]
    (sig,) = signals(res)
    assert sig.direction is Direction.SHORT
    assert sig.level_name == "ORL" and sig.level_value == 95
    assert sig.entry_price == 92.5          # reference (prev high 95.5) not traded -> close
    assert sig.stop_level == 95 and sig.stop_on == "close"
    assert sig.target_ref == "S2"


def test_orb_short_fills_at_previous_candle_high_when_traded() -> None:
    specs = list(SHORT_ORB)
    specs[2] = (94.5, 96, 92, 92.5)          # spikes to 96, through the reference 95.5
    (sig,) = signals(run(specs))
    assert sig.entry_price == 95.5


# ---- Case 1: ORB never confirmed -> bull trap ----------------------------------------------
def test_case_1_aggressive_entry_at_close_back_inside() -> None:
    specs = [
        (100, 112, 100, 111),    # 0 sign
        (111, 111, 108, 109),    # 1 closes back below ORH before any confirmation
    ]
    res = run(specs)
    assert PatternKind.FAILED_CASE_1 in kinds(res[1])
    (sig,) = signals(res)
    assert sig.setup is Setup.FOLLOWUP_CASE_1
    assert sig.direction is Direction.SHORT
    assert sig.aggressive and sig.case == 1
    assert sig.entry_price == 109
    assert sig.stop_on == "none" and sig.stop_level is None
    assert sig.target_ref == "ORL"
    assert sig.candle_start == at(9, 24)


def test_bull_trap_reverse_retest_entry_on_break_of_pullback_low() -> None:
    specs = [
        (100, 112, 100, 111),      # 0 sign
        (111, 111, 108, 109),      # 1 failure -> trap sign (red)
        (108, 109.8, 107.5, 109.5),  # 2 green pullback candle; does not touch ORH
        (109, 109.2, 107, 107.2),  # 3 breaks the pullback low 107.5
    ]
    res = run(specs)
    standard = signals(res, Setup.TRAP_REVERSE_RETEST)
    assert len(standard) == 1
    sig = standard[0]
    assert sig.direction is Direction.SHORT
    assert sig.entry_price == 107.5
    assert sig.stop_level == 110 and sig.stop_on == "touch"
    assert sig.target_ref == "ORL"
    assert sig.case == 1 and not sig.aggressive
    assert sig.candle_start == at(9, 30)
    assert PatternKind.TRAP_PULLBACK in kinds(res[2])
    assert PatternKind.TRAP_TRIGGERED in kinds(res[3])


def test_aggressive_and_standard_trap_signals_share_a_sign_id() -> None:
    specs = [
        (100, 112, 100, 111), (111, 111, 108, 109),
        (108, 109.8, 107.5, 109.5), (109, 109.2, 107, 107.2),
    ]
    aggressive = signals(run(specs), Setup.FOLLOWUP_CASE_1)[0]
    standard = signals(run(specs), Setup.TRAP_REVERSE_RETEST)[0]
    assert aggressive.sign_id == standard.sign_id
    assert aggressive.signal_id != standard.signal_id


def test_trap_gap_through_pullback_low_fills_at_open() -> None:
    specs = [
        (100, 112, 100, 111), (111, 111, 108, 109),
        (108, 109.8, 107.5, 109.5),
        (106.5, 107, 106, 106.8),   # opens below the pullback low
    ]
    sig = signals(run(specs), Setup.TRAP_REVERSE_RETEST)[0]
    assert sig.entry_price == 106.5
    assert "gap" in sig.entry_basis


def test_trap_uses_most_recent_green_candle_as_pullback() -> None:
    specs = [
        (100, 112, 100, 111), (111, 111, 108, 109),
        (108, 109.8, 107.5, 109.5),     # green, low 107.5
        (109.5, 109.9, 108.0, 109.8),   # green again (low 108 > 107.5 so no break), new pullback
        (109.5, 109.6, 107.9, 108.0),   # breaks 108.0 -> entry at 108
    ]
    sig = signals(run(specs), Setup.TRAP_REVERSE_RETEST)[0]
    assert sig.entry_price == 108.0


def test_trap_cancelled_by_new_close_beyond_level() -> None:
    specs = [
        (100, 112, 100, 111), (111, 111, 108, 109),
        (109, 111.5, 108.5, 111),   # closes above ORH again
        (111, 111, 100, 101),       # would have broken lows, but trap is dead
    ]
    res = run(specs)
    assert PatternKind.TRAP_CANCELLED in kinds(res[2])
    assert signals(res, Setup.TRAP_REVERSE_RETEST) == []


def test_trap_entry_fires_only_once() -> None:
    specs = [
        (100, 112, 100, 111), (111, 111, 108, 109),
        (108, 109.8, 107.5, 109.5), (109, 109.2, 107, 107.2),
        (107.2, 107.3, 105, 105.5),
    ]
    assert len(signals(run(specs), Setup.TRAP_REVERSE_RETEST)) == 1


# ---- Case 2: ORB confirmed, then failed ----------------------------------------------------
def test_case_2_after_confirmed_orb() -> None:
    specs = LONG_ORB + [(112, 112.5, 108, 109)]   # 3 closes back below ORH
    res = run(specs)
    assert len(signals(res, Setup.ORB_BREAKOUT)) == 1
    assert PatternKind.FAILED_CASE_2 in kinds(res[3])
    (case2,) = signals(res, Setup.FOLLOWUP_CASE_2)
    assert case2.direction is Direction.SHORT
    assert case2.entry_price == 109 and case2.case == 2 and case2.aggressive
    assert case2.stop_on == "none"


# ---- bear trap (mirror of the bull trap) ---------------------------------------------------
def test_bear_trap_case_1_and_reverse_retest() -> None:
    specs = [
        (96, 97, 93, 94),            # 0 sign: close 94 < ORL 95
        (94, 95.6, 93.8, 95.5),      # 1 closes back above ORL, no confirmation -> Case 1
        (96, 96.5, 95.2, 95.7),      # 2 red pullback candle (does not touch ORL)
        (95.8, 96.6, 95.7, 96.4),    # 3 breaks the pullback high 96.5
    ]
    res = run(specs)
    (aggressive,) = signals(res, Setup.FOLLOWUP_CASE_1)
    assert aggressive.direction is Direction.LONG
    assert aggressive.entry_price == 95.5
    assert aggressive.target_ref == "ORH"
    (standard,) = signals(res, Setup.TRAP_REVERSE_RETEST)
    assert standard.direction is Direction.LONG
    assert standard.entry_price == 96.5
    assert standard.stop_level == 95 and standard.stop_on == "touch"
    assert standard.target_ref == "ORH"


# ---- R2 / S2 as OR levels ------------------------------------------------------------------
PIV = traditional_pivots(110, 90, 100)   # P=100, R2=120, S2=80


def test_pivot_values_used_by_the_scenarios() -> None:
    assert (PIV.r2, PIV.s2) == (120, 80)


def test_r2_traded_as_orh_with_r4_target() -> None:
    specs = [
        (118, 121.5, 117, 121),      # sign above R2
        (121, 122, 119.5, 120.5),    # retest
        (120.5, 123, 120.2, 122.5),  # confirmation
    ]
    sigs = signals(run(specs, PIV), Setup.PIVOT_BREAKOUT)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.level_name == "R2" and sig.direction is Direction.LONG
    assert sig.target_ref == "R4"
    assert sig.stop_level == 120 and sig.stop_on == "close"


def test_s2_traded_as_orl_with_s4_target() -> None:
    specs = [
        (82, 83, 78.5, 79),          # sign below S2 (80)
        (79, 80.5, 78.6, 79.5),      # retest
        (79.5, 79.8, 77, 77.5),      # confirmation
    ]
    sigs = signals(run(specs, PIV), Setup.PIVOT_BREAKOUT)
    assert len(sigs) == 1
    assert sigs[0].level_name == "S2" and sigs[0].direction is Direction.SHORT
    assert sigs[0].target_ref == "S4"


def test_failed_r2_break_creates_no_trap_or_followup() -> None:
    specs = [
        (118, 121.5, 117, 121),      # sign above R2
        (121, 121.2, 118, 119),      # fails
        (119, 119.5, 116, 116.5),
    ]
    res = run(specs, PIV)
    assert signals(res, Setup.FOLLOWUP_CASE_1) == []
    assert signals(res, Setup.TRAP_REVERSE_RETEST) == []


# ---- config flags, determinism -------------------------------------------------------------
def test_disabled_setups_emit_no_signals() -> None:
    specs = LONG_ORB + [(112, 112.5, 108, 109)]
    off = EnabledSetups(orb=False, followup_failure=False)
    assert signals(run(specs, enabled=off)) == []


def test_trap_flag_only_blocks_reverse_retest_entries() -> None:
    specs = [
        (100, 112, 100, 111), (111, 111, 108, 109),
        (108, 109.8, 107.5, 109.5), (109, 109.2, 107, 107.2),
    ]
    res = run(specs, enabled=EnabledSetups(trap=False))
    assert len(signals(res, Setup.FOLLOWUP_CASE_1)) == 1
    assert signals(res, Setup.TRAP_REVERSE_RETEST) == []


def test_signal_id_is_deterministic() -> None:
    first = signals(run(LONG_ORB))[0].signal_id
    second = signals(run(LONG_ORB))[0].signal_id
    assert first == second and len(first) == 16


def test_detector_rejects_candles_from_another_day() -> None:
    tracker = OpeningRangeTracker()
    detector = CoreSetupDetector(EnabledSetups())
    day1 = series(BASE)[0]
    tracker.update(day1)
    detector.update(day1, None, None)
    other = series(BASE, first=at(9, 15, date(2026, 10, 16)))[0]
    with pytest.raises(ValueError):
        detector.update(other, None, None)
