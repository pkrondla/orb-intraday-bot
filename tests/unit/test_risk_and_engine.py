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
from orb_intraday_bot.risk.targets import plan_targets
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
def test_orb_stop_sits_on_the_level_when_distance_is_in_band() -> None:
    plan = plan_stop(make(entry=130, stop_level=110), STOPS)
    assert plan.stop_points == 20
    assert plan.price_stop == 110
    assert plan.close_exit_level == 110
    assert plan.basis == "ORH is 20.0 pts away"


def test_orb_stop_accepts_the_floor_and_the_cap() -> None:
    at_floor = plan_stop(make(entry=120, stop_level=110), STOPS)
    assert at_floor.stop_points == 10 and at_floor.price_stop == 110
    at_cap = plan_stop(make(entry=145, stop_level=110), STOPS)
    assert at_cap.stop_points == 35 and at_cap.price_stop == 110


def test_orb_stop_rejects_a_fill_inside_the_floor() -> None:
    with pytest.raises(PlanError, match="below the 10-point floor"):
        plan_stop(make(entry=112, stop_level=110), STOPS)


def test_orb_stop_rejects_a_fill_beyond_the_cap() -> None:
    with pytest.raises(PlanError, match="above the 35-point cap"):
        plan_stop(make(entry=160, stop_level=110), STOPS)


def test_orb_stop_rejects_entry_already_beyond_the_level() -> None:
    with pytest.raises(PlanError, match="already beyond ORH"):
        plan_stop(make(entry=109, stop_level=110), STOPS)


def test_orb_short_stop_sits_on_orl() -> None:
    plan = plan_stop(
        make(direction=Direction.SHORT, entry=80, stop_level=95, level=95, level_name="ORL"),
        STOPS,
    )
    assert plan.stop_points == 15
    assert plan.price_stop == 95
    assert plan.close_exit_level == 95


def test_pivot_breakout_uses_the_same_level_bound_stop() -> None:
    plan = plan_stop(
        make(setup=Setup.PIVOT_BREAKOUT, entry=135, stop_level=120, level=120, level_name="R2"),
        STOPS,
    )
    assert plan.stop_points == 15 and plan.price_stop == 120 and plan.close_exit_level == 120
    with pytest.raises(PlanError, match="above the 35-point cap"):
        plan_stop(
            make(setup=Setup.PIVOT_BREAKOUT, entry=160, stop_level=120, level=120, level_name="R2"),
            STOPS,
        )


def test_additional_whipsaw_close_stop_still_relocates_to_35() -> None:
    plan = plan_stop(
        make(setup=Setup.ADDITIONAL_WHIPSAW, entry=130, stop_level=110, stop_on="close"),
        STOPS,
    )
    assert plan.stop_points == 20
    assert plan.price_stop == 95                         # entry - 35, in front of the level
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


def test_orb_long_targets_r2_with_nearest_pivot_as_t1() -> None:
    t = targets(make(entry=105))
    assert t.t2 == 120 and t.t2_points == 15
    assert t.t1 == 110 and "R1" in t.t1_basis


def test_t1_is_halfway_when_no_pivot_between() -> None:
    t = targets(make(entry=121), pivots=WIDE)          # R2 140; R1 120 is behind the entry
    assert t.t2 == 140
    assert t.t1 == pytest.approx(130.5) and t.t1_basis == "halfway to T2"


def test_orb_short_targets_s2() -> None:
    t = targets(make(direction=Direction.SHORT, entry=95, level=95, stop_level=95,
                     target_ref="S2", level_name="ORL"))
    assert t.t2 == 80 and t.t1 == 90 and "S1" in t.t1_basis


def test_entry_already_beyond_r2_targets_r4() -> None:
    t = targets(make(entry=125))
    assert t.t2 == 140 and "R4" in t.t2_basis
    assert t.t1 == 130                                   # R3 is between 125 and 140


def test_pivot_breakout_targets_r4() -> None:
    t = targets(make(setup=Setup.PIVOT_BREAKOUT, entry=121, target_ref="R4", level=120,
                     stop_level=120, level_name="R2"))
    assert t.t2 == 140


def test_target_is_capped_at_r2_for_trap_setups() -> None:
    # bear trap long, target ORH = 135 but R2 (120) closes all positions first
    t = targets(make(setup=Setup.TRAP_REVERSE_RETEST, entry=105, target_ref="ORH"), orh=135)
    assert t.t2 == 120 and "capped at R2" in t.t2_basis


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
    assert far_projection.t2 == 120 and "pivot R2" in far_projection.t2_basis
    near_projection = targets(make(**base, target_price=116))       # type: ignore[arg-type]
    assert near_projection.t2 == 116 and "projection" in near_projection.t2_basis


def test_consolidation_short_target() -> None:
    sig = make(setup=Setup.CONSOLIDATION_BREAKOUT, direction=Direction.SHORT, entry=95,
               target_ref="OR_PROJECTION_OR_PIVOT", target_price=70, stop_level=None,
               stop_on="none", level_name="ORL")
    assert targets(sig).t2 == 80                          # S2 is nearer than the projection


def test_range_target_without_pivots_uses_the_signal_price() -> None:
    sig = make(setup=Setup.FAKE_BREAK, entry=100, target_ref="RANGE_OPPOSITE_END",
               target_price=130, stop_level=90, stop_on="touch")
    t = targets(sig, pivots=None)
    assert t.t2 == 130 and t.t1 == 115 and t.t1_basis == "halfway to T2"
    with pytest.raises(PlanError):
        targets(make(target_ref="RANGE_OPPOSITE_END"))    # no price supplied


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
        (Zone.BULLISH, Direction.SHORT, False, True),
        (Zone.BULLISH, Direction.LONG, False, False),
        (Zone.BULLISH, Direction.LONG, True, False),
        (Zone.BEARISH, Direction.LONG, False, True),
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
    assert trade.stop.stop_points == 15 and trade.targets.t2 == 140
    assert trade.stop.price_stop == 110 and trade.stop.close_exit_level == 110
    assert trade.risk_budget_inr == 5000


def test_engine_zone_block_is_reported() -> None:
    short = make(direction=Direction.SHORT, entry=125, level=130, stop_level=130,
                 target_ref="S2", sign="short")
    res = engine().evaluate(bar(125), [short], 110, 95)
    assert res.accepted == []
    assert [(r.stage, r.reason) for r in res.rejected] == [("zone", "above R1: no shorts")]


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


def test_engine_skips_orb_when_the_level_is_too_far() -> None:
    far = make(entry=160, stop_level=110, level=110, sign="far")
    res = engine().evaluate(bar(160), [far], 110, 95)
    assert res.accepted == []
    assert res.rejected[0].stage == "plan"
    assert "above the 35-point cap" in res.rejected[0].reason


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
