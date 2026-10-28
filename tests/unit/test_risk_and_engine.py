"""Phase 5: stop plan, target plan, sizing, zone filter, signal engine / arbiter."""

from __future__ import annotations

import pytest

from orb_intraday_bot.config.settings import (
    ArbiterConfig,
    SizingConfig,
    StopsConfig,
    StrategyConfig,
)
from orb_intraday_bot.core.models import Direction, Zone
from orb_intraday_bot.core.signals import Setup, SetupSignal, StopOn
from orb_intraday_bot.engine.signal_engine import SignalEngine
from orb_intraday_bot.engine.zone_filter import zone_block_reason
from orb_intraday_bot.indicators.pivots import traditional_pivots
from orb_intraday_bot.risk.sizing import lots_for, risk_budget_inr
from orb_intraday_bot.risk.stops import PlanError, plan_stop
from orb_intraday_bot.risk.targets import plan_targets, t2_trigger_price
from tests.helpers import at, candle

# Narrow pivots: S5 50 S4 60 S3 70 S2 80 S1 90 P 100 R1 110 R2 120 R3 130 R4 140 R5 150
NARROW = traditional_pivots(110, 90, 100)
# Wide pivots: S2 60 S1 80 P 100 R1 120 R2 140 R3 160 R4 180
WIDE = traditional_pivots(120, 80, 100)
STOPS = StopsConfig()    # 35 hard, 10 floor


def make(
    *,
    setup: Setup = Setup.ORB_BREAKOUT,
    direction: Direction = Direction.LONG,
    entry: float = 112,
    level: float = 110,
    stop_level: float | None = 110,
    stop_on: StopOn = "close",
    target_ref: str | None = "R2",
    target_price: float | None = None,
    aggressive: bool = False,
    sign: str = "s1",
    level_name: str = "ORH",
    start_minute: int = 0,
) -> SetupSignal:
    return SetupSignal(
        setup=setup, direction=direction, level_name=level_name, level_value=level,
        candle_start=at(10, start_minute), entry_price=entry, entry_basis="test",
        sign_id=sign, stop_level=stop_level, stop_on=stop_on, target_ref=target_ref,
        target_price=target_price, aggressive=aggressive,
    )


# ---- stop plan -----------------------------------------------------------------------------
def test_orb_stop_is_35_points_plus_close_through_the_level() -> None:
    plan = plan_stop(make(entry=130, stop_level=110), STOPS)
    assert plan.stop_points == 35
    assert plan.price_stop == 95
    assert plan.close_exit_level == 110
    assert "close through ORH" in plan.basis


def test_orb_stop_still_takes_a_close_level_and_a_far_level() -> None:
    close = plan_stop(make(entry=112, stop_level=110), STOPS)
    assert close.stop_points == 35 and close.price_stop == 77 and close.close_exit_level == 110
    far = plan_stop(make(entry=160, stop_level=110), STOPS)
    assert far.stop_points == 35 and far.price_stop == 125 and far.close_exit_level == 110


def test_orb_stop_rejects_entry_already_beyond_the_level() -> None:
    with pytest.raises(PlanError, match="already beyond ORH"):
        plan_stop(make(entry=109, stop_level=110), STOPS)


def test_orb_short_stop_is_35_plus_close_through_orl() -> None:
    plan = plan_stop(
        make(direction=Direction.SHORT, entry=80, stop_level=95, level=95, level_name="ORL"),
        STOPS,
    )
    assert plan.stop_points == 35
    assert plan.price_stop == 115
    assert plan.close_exit_level == 95


def test_pivot_breakout_uses_the_same_close_or_35_stop() -> None:
    plan = plan_stop(
        make(setup=Setup.PIVOT_BREAKOUT, entry=135, stop_level=120, level=120, level_name="R2"),
        STOPS,
    )
    assert plan.stop_points == 35 and plan.price_stop == 100 and plan.close_exit_level == 120
    far = plan_stop(
        make(setup=Setup.PIVOT_BREAKOUT, entry=160, stop_level=120, level=120, level_name="R2"),
        STOPS,
    )
    assert far.stop_points == 35 and far.close_exit_level == 120


def test_additional_whipsaw_close_stop_is_35_plus_close_through() -> None:
    plan = plan_stop(
        make(setup=Setup.ADDITIONAL_WHIPSAW, entry=130, stop_level=110, stop_on="close"),
        STOPS,
    )
    assert plan.stop_points == 35
    assert plan.price_stop == 95
    assert plan.close_exit_level == 110


def test_touch_stop_levels() -> None:
    near = plan_stop(make(setup=Setup.TRAP_REVERSE_RETEST, entry=107.5, stop_level=110,
                          stop_on="touch", direction=Direction.SHORT), STOPS)
    assert near.stop_points == 10 and near.price_stop == 117.5 and near.close_exit_level is None
    mid = plan_stop(make(setup=Setup.TRAP_REVERSE_RETEST, entry=100, stop_level=80,
                          stop_on="touch"), STOPS)
    assert mid.stop_points == 20 and mid.price_stop == 80
    far = plan_stop(make(setup=Setup.TRAP_REVERSE_RETEST, entry=100, stop_level=40,
                          stop_on="touch"), STOPS)
    assert far.stop_points == 35 and far.price_stop == 65


def test_touch_stop_on_the_wrong_side_is_rejected() -> None:
    with pytest.raises(PlanError):
        plan_stop(make(setup=Setup.TRAP_REVERSE_RETEST, entry=100, stop_level=105,
                        stop_on="touch"), STOPS)


def test_no_structural_stop_means_hard_stop() -> None:
    plan = plan_stop(make(stop_level=None, stop_on="none"), STOPS)
    assert plan.stop_points == 35 and plan.price_stop == 112 - 35
    short = plan_stop(make(direction=Direction.SHORT, entry=100, stop_level=None,
                           stop_on="none"), STOPS)
    assert short.price_stop == 135


# ---- target plan ---------------------------------------------------------------------------
def targets(sig: SetupSignal, pivots=NARROW, orh=110.0, orl=95.0):  # type: ignore[no-untyped-def]
    return plan_targets(sig, pivots, orh, orl, STOPS)


def test_t2_trigger_price_sits_a_few_points_in_front() -> None:
    assert t2_trigger_price(
        22800, 22900, long=True, proximity_points=3, min_stop_points=10
    ) == 22897
    assert t2_trigger_price(
        22800, 22700, long=False, proximity_points=3, min_stop_points=10
    ) == 22703
    assert t2_trigger_price(
        22800, 22900, long=True, proximity_points=0, min_stop_points=10
    ) == 22900


def test_t2_trigger_shrinks_so_the_level_keeps_ten_points_of_room() -> None:
    # T2 is 12 pts away: buffer is min(3, 2) = 2, trigger 10 pts from entry.
    assert t2_trigger_price(
        22800, 22812, long=True, proximity_points=3, min_stop_points=10
    ) == 22810
    # T2 is exactly 10 pts: no buffer, exact touch.
    assert t2_trigger_price(
        22800, 22810, long=True, proximity_points=3, min_stop_points=10
    ) == 22810


def test_orb_long_t1_is_r2() -> None:
    t = targets(make(entry=105))
    assert t.t1 == 120 and t.t1_points == 15
    assert "R2" in t.t1_basis
    # OR-range 15 → 135; R3 130 is nearer.
    assert t.t2 == 130 and "R3" in t.t2_basis


def test_orb_long_t1_is_r2_when_no_pivot_between() -> None:
    t = targets(make(entry=121), pivots=WIDE)          # R2 140; R1 120 is behind the entry
    assert t.t1 == 140
    assert t.t2 == 155 and "OR-range" in t.t2_basis    # 140 + 15; R3 160 is farther


def test_orb_short_t1_is_s2() -> None:
    t = targets(make(direction=Direction.SHORT, entry=95, level=95, stop_level=95,
                     target_ref="S2", level_name="ORL"))
    assert t.t1 == 80
    assert t.t2 == 70 and "S3" in t.t2_basis           # 80 − 15 = 65; S3 70 is nearer


def test_entry_already_beyond_r2_targets_r4() -> None:
    t = targets(make(entry=125))
    assert t.t1 == 140 and "R4" in t.t1_basis
    assert t.t2 == 150 and "R5" in t.t2_basis          # 140 + 15 = 155; R5 150 is nearer


def test_pivot_breakout_targets_r4() -> None:
    t = targets(make(setup=Setup.PIVOT_BREAKOUT, entry=121, target_ref="R4", level=120,
                     stop_level=120, level_name="R2"))
    assert t.t1 == 140
    assert t.t2 == 150 and "R5" in t.t2_basis


def test_target_is_capped_at_r2_for_trap_setups() -> None:
    # bear trap long, T1 capped at R2; runner may go past R2
    t = targets(make(setup=Setup.TRAP_REVERSE_RETEST, entry=105, target_ref="ORH"), orh=135)
    assert t.t1 == 120 and "capped at R2" in t.t1_basis
    assert t.t2 == 130 and "R3" in t.t2_basis


def test_orl_on_s2_is_one_magnet() -> None:
    # 28 Oct 2026: Case 2 short at 22671, ORL 22635 sitting on S2.
    sig = make(
        setup=Setup.FOLLOWUP_CASE_2, direction=Direction.SHORT, entry=75,
        target_ref="ORL", stop_level=None, stop_on="none", level_name="ORH", aggressive=True,
    )
    t = plan_targets(sig, WIDE, 110.0, 62.0, STOPS, zone=Zone.BEARISH)
    assert t.t1 == 62 and "≈ S2" in t.t1_basis
    assert t.t2 == 40 and "S3" in t.t2_basis           # 62 − 48 = 14; S3 40 is nearer


def test_case2_short_caps_at_s2_when_orl_is_well_beyond() -> None:
    sig = make(
        setup=Setup.FOLLOWUP_CASE_2, direction=Direction.SHORT, entry=95,
        target_ref="ORL", stop_level=None, stop_on="none", level_name="ORH", aggressive=True,
    )
    t = plan_targets(sig, WIDE, 110.0, 50.0, STOPS, zone=Zone.BEARISH)
    assert t.t1 == 60 and "capped at S2" in t.t1_basis
    assert t.t2 == 40 and "S3" in t.t2_basis


def test_orh_target_below_entry_is_rejected() -> None:
    with pytest.raises(PlanError):
        targets(make(setup=Setup.TRAP_REVERSE_RETEST, entry=112, target_ref="ORH"), orh=108)


def test_entry_beyond_r4_has_no_room() -> None:
    with pytest.raises(PlanError):
        targets(make(entry=141))


def test_target_closer_than_the_floor_is_rejected() -> None:
    with pytest.raises(PlanError, match="pts away"):
        targets(make(entry=115))                         # R2 is 5 away


def test_unset_or_level_or_missing_pivots_are_rejected() -> None:
    with pytest.raises(PlanError):
        targets(make(setup=Setup.TRAP_REVERSE_RETEST, entry=105, target_ref="ORH"), orh=None)
    with pytest.raises(PlanError):
        targets(make(entry=105), pivots=None)


def test_consolidation_target_is_the_nearer_of_projection_and_pivot() -> None:
    base = dict(setup=Setup.CONSOLIDATION_BREAKOUT, entry=105,
                target_ref="OR_PROJECTION_OR_PIVOT", stop_level=None, stop_on="none")
    # pivot candidates must be at least 10 points away: R1 (110) is only 5 away, so R2 (120)
    far_projection = targets(make(**base, target_price=125))        # type: ignore[arg-type]
    assert far_projection.t1 == 120 and "pivot R2" in far_projection.t1_basis
    near_projection = targets(make(**base, target_price=116))       # type: ignore[arg-type]
    assert near_projection.t1 == 116 and "projection" in near_projection.t1_basis


def test_consolidation_short_target() -> None:
    sig = make(setup=Setup.CONSOLIDATION_BREAKOUT, direction=Direction.SHORT, entry=95,
               target_ref="OR_PROJECTION_OR_PIVOT", target_price=70, stop_level=None,
               stop_on="none", level_name="ORL")
    assert targets(sig).t1 == 80                          # S2 is nearer than the projection


def test_range_target_without_pivots_uses_the_signal_price() -> None:
    sig = make(setup=Setup.FAKE_BREAK, entry=100, target_ref="RANGE_OPPOSITE_END",
               target_price=130, stop_level=90, stop_on="touch")
    t = plan_targets(sig, None, None, None, STOPS)
    assert t.t1 == 130 and t.t2 == 130
    with pytest.raises(PlanError):
        targets(make(target_ref="RANGE_OPPOSITE_END"))    # no price supplied


# 26 Oct 2026 session levels (exchange close). Neutral is S1 22390.6 .. R1 22807.1.
OCT26 = traditional_pivots(22851.5, 22435.0, 22576.6)


def test_neutral_zone_3ll_t1_is_p() -> None:
    sig = make(
        setup=Setup.STRUCTURE_WHIPSAW, entry=22546.4, stop_level=22500.0, stop_on="touch",
        target_ref="R2", level_name="3LL", level=22500.0,
    )
    t = plan_targets(sig, OCT26, 22575.0, 22444.6, STOPS, zone=Zone.NEUTRAL)
    assert t.t1 == pytest.approx(22621.0333, abs=0.05) and t.t1_basis.startswith("P")
    assert t.t2 == pytest.approx(t.t1 + (22575.0 - 22444.6), abs=0.05)
    setup = plan_targets(sig, OCT26, 22575.0, 22444.6, STOPS)
    assert setup.t1 == pytest.approx(23037.5, abs=0.05)
    assert setup.t2 != setup.t1


def test_neutral_zone_short_t1_is_p() -> None:
    sig = make(
        direction=Direction.SHORT, entry=22700.0, stop_level=22740.0, stop_on="touch",
        target_ref="S2", level_name="2HH", level=22740.0, setup=Setup.STRUCTURE_WHIPSAW,
    )
    t = plan_targets(sig, OCT26, 22575.0, 22650.0, STOPS, zone=Zone.NEUTRAL)
    assert t.t1 == pytest.approx(22621.0333, abs=0.05) and t.t1_basis.startswith("P")
    assert t.t2 == pytest.approx(22390.5666, abs=0.05) and "S1" in t.t2_basis


def test_neutral_zone_above_p_t1_is_r1() -> None:
    sig = make(entry=22650.0, stop_level=22620.0, target_ref="R2")
    t = plan_targets(sig, OCT26, 22700.0, 22444.6, STOPS, zone=Zone.NEUTRAL)
    assert t.t1 == pytest.approx(22807.0666, abs=0.05) and t.t1_basis.startswith("R1")
    assert t.t2 == t.t1


def test_neutral_zone_rejects_when_the_zone_edge_is_too_close() -> None:
    # WIDE: R1 120. Entry 112 is still neutral; R1 is only 8 pts away, P is behind.
    sig = make(entry=112, stop_level=95, target_ref="R2")
    with pytest.raises(PlanError, match="neutral zone: no pivot"):
        plan_targets(sig, WIDE, 116.0, 80.0, STOPS, zone=Zone.NEUTRAL)


def test_bullish_and_bearish_zones_keep_the_setup_targets() -> None:
    long = plan_targets(make(entry=121, stop_level=110), WIDE, 110.0, 95.0, STOPS,
                        zone=Zone.BULLISH)
    assert long.t1 == 140 and long.t2 == 155
    short = plan_targets(
        make(direction=Direction.SHORT, entry=70, level=95, stop_level=95, target_ref="S2",
             level_name="ORL"),
        WIDE, 110.0, 95.0, STOPS, zone=Zone.BEARISH,
    )
    assert short.t1 == 60 and short.t2 == 45               # 60 − 15; S3 40 is farther


# ---- sizing --------------------------------------------------------------------------------
def test_risk_budget_and_lots() -> None:
    assert risk_budget_inr(SizingConfig()) == 5000
    assert lots_for(5000, 19.25, 65) == 3                  # floor(5000 / 1251.25)
    assert lots_for(5000, 100, 65) == 0                    # stop too wide for one lot
    assert lots_for(0, 19, 65) == 0 and lots_for(5000, 0, 65) == 0 and lots_for(5000, 19, 0) == 0


# ---- zone filter ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("zone", "direction", "aggressive", "blocked"),
    [
        (Zone.BULLISH, Direction.SHORT, False, False),
        (Zone.BULLISH, Direction.LONG, False, False),
        (Zone.BULLISH, Direction.LONG, True, False),
        (Zone.BEARISH, Direction.LONG, False, False),
        (Zone.BEARISH, Direction.SHORT, True, False),
        (Zone.NEUTRAL, Direction.LONG, True, True),
        (Zone.NEUTRAL, Direction.SHORT, True, True),
        (Zone.NEUTRAL, Direction.LONG, False, False),
        (Zone.NEUTRAL, Direction.SHORT, False, False),
    ],
)
def test_zone_rules(zone: Zone, direction: Direction, aggressive: bool, blocked: bool) -> None:
    reason = zone_block_reason(make(direction=direction, aggressive=aggressive), zone)
    assert (reason is not None) is blocked


# ---- signal engine -------------------------------------------------------------------------
def engine(cfg: StrategyConfig | None = None, pivots=WIDE) -> SignalEngine:  # type: ignore[no-untyped-def]
    return SignalEngine(cfg or StrategyConfig(), pivots)


def bar(close: float):  # type: ignore[no-untyped-def]
    return candle(at(10, 0), close, close + 1, close - 1, close)


# Wide pivots: neutral zone is 80..120; bullish above 120; bearish below 80
LONG_ORB = make(entry=125, stop_level=110, level=110, target_ref="R2", sign="orb")


def test_engine_accepts_a_valid_signal_and_builds_the_plan() -> None:
    res = engine().evaluate(bar(125), [LONG_ORB], 110, 95)
    assert res.zone is Zone.BULLISH and res.rejected == []
    (trade,) = res.accepted
    assert trade.stop.stop_points == 35 and trade.targets.t1 == 140
    assert trade.targets.t2 == 155
    assert trade.stop.price_stop == 90 and trade.stop.close_exit_level == 110
    assert trade.risk_budget_inr == 5000


def test_engine_applies_neutral_zone_targets() -> None:
    sig = make(
        setup=Setup.STRUCTURE_WHIPSAW, entry=100, stop_level=85, stop_on="touch",
        target_ref="R2", sign="st", level_name="3LL", level=85,
    )
    res = engine().evaluate(bar(100), [sig], 110, 95)
    assert res.zone is Zone.NEUTRAL
    t = res.accepted[0].targets
    assert t.t1 == pytest.approx(120) and t.t1_basis.startswith("R1")
    assert t.t2 == t.t1


def test_engine_takes_a_short_above_r1() -> None:
    short = make(direction=Direction.SHORT, entry=125, level=130, stop_level=130,
                 target_ref="S2", sign="short")
    res = engine().evaluate(bar(125), [short], 110, 95)
    assert res.zone is Zone.BULLISH and res.rejected == []
    assert res.accepted[0].signal.direction is Direction.SHORT


def test_engine_takes_a_long_below_s1() -> None:
    long = make(entry=70, stop_level=55, level=55, target_ref="R2", sign="long")
    res = engine().evaluate(bar(70), [long], 110, 95)
    assert res.zone is Zone.BEARISH and res.rejected == []
    assert res.accepted[0].signal.direction is Direction.LONG


def test_blocked_aggressive_signal_does_not_use_up_its_sign() -> None:
    eng = engine()
    aggressive = make(setup=Setup.FOLLOWUP_CASE_1, entry=100, stop_level=None, stop_on="none",
                      aggressive=True, sign="trap1", target_ref="R2", level_name="ORH",
                      direction=Direction.LONG)
    standard = make(setup=Setup.TRAP_REVERSE_RETEST, entry=100, stop_level=90, stop_on="touch",
                    sign="trap1", target_ref="R2", direction=Direction.LONG)
    first = eng.evaluate(bar(100), [aggressive], None, None)         # neutral zone
    assert first.accepted == [] and first.rejected[0].stage == "zone"
    second = eng.evaluate(bar(100), [standard], None, None)
    assert len(second.accepted) == 1
    again = eng.evaluate(bar(100), [standard], None, None)
    assert again.rejected[0].stage == "duplicate"


def test_one_trade_per_sign_id() -> None:
    eng = engine()
    assert len(eng.evaluate(bar(125), [LONG_ORB], 110, 95).accepted) == 1
    res = eng.evaluate(bar(126), [LONG_ORB], 110, 95)
    assert res.accepted == [] and res.rejected[0].reason == "sign already traded"


def test_plan_failure_is_reported() -> None:
    bad = make(entry=125, target_ref="R2", sign="x", stop_level=110)
    res = engine().evaluate(bar(125), [bad], 110, 95, ())            # fine
    assert len(res.accepted) == 1
    too_close = make(entry=138, sign="y")                            # R2 (140) only 2 away
    res = engine().evaluate(bar(138), [too_close], 110, 95)
    assert res.rejected[0].stage == "plan"


def test_engine_still_takes_orb_when_the_level_is_far() -> None:
    far = make(entry=160, stop_level=110, level=110, sign="far")
    res = engine().evaluate(bar(160), [far], 110, 95)
    assert len(res.accepted) == 1
    assert res.accepted[0].stop.price_stop == 125
    assert res.accepted[0].stop.close_exit_level == 110


def test_priority_resolves_a_same_candle_conflict() -> None:
    long_orb = make(entry=100, stop_level=90, level=90, target_ref="R2", sign="orb",
                    level_name="ORH")
    short_struct = make(setup=Setup.STRUCTURE_WHIPSAW, direction=Direction.SHORT, entry=100,
                        stop_level=110, stop_on="touch", target_ref="S2", sign="st",
                        level_name="2HH", level=110)
    for order in ([long_orb, short_struct], [short_struct, long_orb]):
        res = engine().evaluate(bar(100), order, 110, 95)             # neutral zone
        assert [t.signal.sign_id for t in res.accepted] == ["orb"]
        assert res.rejected[0].stage == "arbiter" and "conflicts" in res.rejected[0].reason


def test_priority_order_among_same_direction_signals() -> None:
    a = make(setup=Setup.STRUCTURE_WHIPSAW, entry=100, stop_level=90, stop_on="touch",
             target_ref="R2", sign="st", level_name="2LL", level=90)
    b = make(entry=100, stop_level=90, level=90, target_ref="R2", sign="orb")
    res = engine().evaluate(bar(100), [a, b], 110, 95)
    assert [t.signal.sign_id for t in res.accepted] == ["orb", "st"]   # both long: both taken


def test_opposite_open_position_blocks_unless_allowed() -> None:
    sig = make(entry=100, stop_level=90, level=90, target_ref="R2", sign="orb")
    res = engine().evaluate(bar(100), [sig], 110, 95, [Direction.SHORT])
    assert res.accepted == [] and "SHORT position is open" in res.rejected[0].reason
    cfg = StrategyConfig(arbiter=ArbiterConfig(allow_opposite_positions=True))
    res = engine(cfg).evaluate(bar(100), [sig], 110, 95, [Direction.SHORT])
    assert len(res.accepted) == 1


def test_maximum_open_positions() -> None:
    sig = make(entry=100, stop_level=90, level=90, target_ref="R2", sign="orb")
    res = engine().evaluate(bar(100), [sig], 110, 95, [Direction.LONG] * 5)
    assert "maximum 5 open positions" in res.rejected[0].reason
    res = engine().evaluate(bar(100), [sig], 110, 95, [Direction.LONG] * 4)
    assert len(res.accepted) == 1


def test_daily_trade_limits() -> None:
    cfg = StrategyConfig(
        arbiter=ArbiterConfig(max_trades_per_day=2, max_structure_trades_per_day=1)
    )
    eng = engine(cfg)

    def structure(n: int) -> SetupSignal:
        return make(setup=Setup.STRUCTURE_WHIPSAW, entry=100, stop_level=90, stop_on="touch",
                    target_ref="R2", sign=f"st{n}", level_name="2LL", level=90)

    assert len(eng.evaluate(bar(100), [structure(1)], None, None).accepted) == 1
    res = eng.evaluate(bar(100), [structure(2)], None, None)
    assert "2LL/3LL/2HH/3HH" in res.rejected[0].reason
    orb = make(entry=100, stop_level=90, level=90, target_ref="R2", sign="orb")
    assert len(eng.evaluate(bar(100), [orb], None, None).accepted) == 1
    third = make(entry=100, stop_level=90, level=90, target_ref="R2", sign="orb2")
    res = eng.evaluate(bar(100), [third], None, None)
    assert "daily limit of 2 trades" in res.rejected[0].reason


def test_without_pivots_the_zone_is_neutral_and_pivot_targets_fail() -> None:
    eng = SignalEngine(StrategyConfig(), None)
    assert eng.zone_for(1_000_000) is Zone.NEUTRAL
    res = eng.evaluate(bar(100), [make(entry=100, stop_level=90, target_ref="R2")], 110, 95)
    assert res.rejected[0].stage == "plan"


def test_arbiter_priority_list_is_complete() -> None:
    names = {s.value for s in Setup}
    assert names == set(ArbiterConfig().priority)
