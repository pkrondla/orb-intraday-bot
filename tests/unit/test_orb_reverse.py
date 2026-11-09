"""A confirmed ORB replaces an opposite Case 1/2 on the same level.

ORH 22810 / ORL 22770. The 09:24 close back inside is a Case 1 short. A later close
back above ORH is only a new sign. The short is closed when that breakout confirms,
and the long is opened on that candle. The 35-point stop stays until then.
"""

from __future__ import annotations

from orb_intraday_bot.config.settings import ArbiterConfig, EnabledSetups, StrategyConfig
from orb_intraday_bot.core.errors import ExitFailedError
from orb_intraday_bot.core.models import Direction
from orb_intraday_bot.core.signals import Setup, SetupSignal
from orb_intraday_bot.indicators.pivots import Pivots
from orb_intraday_bot.options.planner import OptionPlanner
from orb_intraday_bot.paper.models import ExitReason
from orb_intraday_bot.paper.ports import EntryExecution
from orb_intraday_bot.runtime.day import TradingDay, followups_to_reverse
from orb_intraday_bot.runtime.report import format_exit_alert
from orb_intraday_bot.runtime.synthetic import SyntheticOptions
from tests.helpers import DAY, at, series

# R1 sits under the fade so the Case 1 is allowed. R2 has room for the later long.
PIVOTS = Pivots(
    p=22800, r1=22804, r2=22960, r3=22980, r4=23000, r5=23020,
    s1=22790, s2=22770, s3=22750, s4=22740, s5=22730,
)
CFG = StrategyConfig(
    enabled_setups=EnabledSetups(
        additional_whipsaw=False,
        consolidation_breakout=False,
        whipsaw_structure=False,
        fake_breakout=False,
    )
)
# 09:15-09:18 fix the range. 09:21 signs ORH, 09:24 fails (Case 1 short).
# 09:30 is a new sign only. 09:33 retests. 09:36 confirms the long.
SEQUENCE = [
    (22800, 22810, 22770, 22805),
    (22805, 22808, 22775, 22800),
    (22808, 22812, 22807, 22811),
    (22811, 22811.5, 22808, 22809),
    (22809, 22809.8, 22806, 22807),
    (22807, 22813, 22807, 22812),
    (22812, 22812.8, 22810.4, 22811.8),
    (22811.8, 22816, 22811.2, 22815),
]


class _FailSell:
    mode = "dry_run"

    def enter(self, trade, plan, now):
        return EntryExecution(price=plan.entry_ltp, lots=plan.lots, order_id="entry")

    def protect(self, position, now) -> None:
        return None

    def sell(self, position, lots, reason, now):
        raise ExitFailedError("sale rejected")

    def poll_stops(self, positions, now):
        return []

    def held_quantities(self):
        return None


def _day(config: StrategyConfig = CFG, executor=None) -> TradingDay:
    return TradingDay(
        config, DAY, "NIFTY-TEST", PIVOTS,
        OptionPlanner(config, SyntheticOptions(65)),
        executor=executor,
        executor_enabled=(lambda: True) if executor is not None else None,
    )


def _run(day: TradingDay, specs=SEQUENCE):
    reports = []
    for candle in series(specs):
        reports.append(day.on_candle(candle))
    return reports


def test_confirmed_orh_long_closes_the_case_1_short_and_opens() -> None:
    day = _day()
    reports = _run(day)
    short_candle = reports[3]
    assert len(short_candle.opened) == 1
    assert short_candle.opened[0].signal.setup is Setup.FOLLOWUP_CASE_1
    assert short_candle.opened[0].signal.direction is Direction.SHORT

    sign_again = reports[5]
    assert sign_again.opened == []
    assert all(
        e.fill is None or e.fill.reason is not ExitReason.ORB_REVERSE for e in sign_again.exits
    )
    assert [p.setup for p, _ in sign_again.marks] == [Setup.FOLLOWUP_CASE_1.value]

    confirmed = reports[7]
    assert confirmed.candle.close == 22815
    reverses = [e for e in confirmed.exits if e.fill and e.fill.reason is ExitReason.ORB_REVERSE]
    assert len(reverses) == 1
    assert reverses[0].fill is not None
    assert reverses[0].fill.futures_price == 22815
    assert "REVERSED" in format_exit_alert(reverses[0])
    assert len(confirmed.opened) == 1
    opened = confirmed.opened[0]
    assert opened.signal.setup is Setup.ORB_BREAKOUT
    assert opened.signal.direction is Direction.LONG
    assert opened.signal.level_name == "ORH"
    assert opened.position is not None
    short = day.paper.closed_positions[0]
    assert short.closed_at == opened.position.opened_at
    assert [p.direction for p in day.paper.open_positions] == [Direction.LONG]
    assert day.signals.trades_today == 2


def test_the_long_then_failing_opens_a_case_2_short() -> None:
    day = _day()
    reports = _run(day, SEQUENCE + [(22815, 22816, 22805, 22806)])
    failed = reports[8]
    assert any(e.fill and e.fill.reason is ExitReason.CLOSE_STOP for e in failed.exits)
    assert len(failed.opened) == 1
    assert failed.opened[0].signal.setup is Setup.FOLLOWUP_CASE_2
    assert failed.opened[0].signal.direction is Direction.SHORT
    assert [p.setup for p in day.paper.open_positions] == [Setup.FOLLOWUP_CASE_2.value]


def test_a_reclaim_that_closes_back_inside_keeps_the_original_short() -> None:
    day = _day()
    specs = list(SEQUENCE)
    specs[6] = (22812, 22812.5, 22807, 22808)   # new sign fails before it can confirm
    reports = _run(day, specs[:7])
    failed = reports[6]
    assert all(
        e.fill is None or e.fill.reason is not ExitReason.ORB_REVERSE for e in failed.exits
    )
    # The new sign is its own Case 1. The original short is not closed to make room for it.
    assert len(failed.opened) == 1
    assert failed.opened[0].signal.setup is Setup.FOLLOWUP_CASE_1
    assert len(day.paper.open_positions) == 2
    assert all(
        p.setup == Setup.FOLLOWUP_CASE_1.value and p.direction is Direction.SHORT
        for p in day.paper.open_positions
    )


def test_an_orb_the_book_cannot_take_does_not_close_the_fade() -> None:
    capped = CFG.model_copy(update={"arbiter": ArbiterConfig(max_trades_per_day=1)})
    day = _day(capped)
    reports = _run(day)
    confirmed = reports[7]
    assert day.signals._cfg.arbiter.max_trades_per_day == 1
    assert confirmed.opened == []
    assert [p.setup for p in day.paper.open_positions] == [Setup.FOLLOWUP_CASE_1.value]
    assert all(
        e.fill is None or e.fill.reason is not ExitReason.ORB_REVERSE for e in confirmed.exits
    )
    # With the short imagined flat, the daily cap is what still blocks the long.
    blocked = next(o.signal for o in confirmed.outcomes if o.signal.setup is Setup.ORB_BREAKOUT)
    assert day.signals.would_accept(confirmed.candle, [blocked], 22810, 22795, []) == []


def test_a_failed_sale_does_not_open_the_long() -> None:
    day = _day(executor=_FailSell())
    reports = _run(day)
    confirmed = reports[7]
    assert confirmed.opened == []
    assert any("SHORT position is open" in o.detail for o in confirmed.outcomes)
    assert [p.setup for p in day.paper.open_positions] == [Setup.FOLLOWUP_CASE_1.value]


def test_catch_up_does_not_reverse_a_position_it_will_not_trade() -> None:
    day = _day()
    candles = series(SEQUENCE)
    for candle in candles[:7]:
        day.on_candle(candle)
    report = day.on_candle(candles[7], allow_open=False)
    assert report.opened == []
    assert [p.setup for p in day.paper.open_positions] == [Setup.FOLLOWUP_CASE_1.value]
    assert all(
        e.fill is None or e.fill.reason is not ExitReason.ORB_REVERSE for e in report.exits
    )


def test_the_other_side_and_a_pivot_break_do_not_close_the_fade() -> None:
    day = _day()
    _run(day, SEQUENCE[:4])
    short = day.paper.open_positions[0]
    candle = series(SEQUENCE)[3]
    orl = SetupSignal(
        setup=Setup.ORB_BREAKOUT, direction=Direction.SHORT, level_name="ORL", level_value=22795,
        candle_start=at(10, 0), entry_price=22780, entry_basis="test", sign_id="orl-new",
        stop_level=22795, stop_on="close", target_ref="S2",
    )
    pivot = SetupSignal(
        setup=Setup.PIVOT_BREAKOUT, direction=Direction.LONG, level_name="R2", level_value=22960,
        candle_start=at(10, 3), entry_price=22961, entry_basis="test", sign_id="r2-new",
        stop_level=22960, stop_on="close", target_ref="R4",
    )
    assert followups_to_reverse(
        day.signals, candle, [orl, pivot], 22810, 22795, day.paper.open_positions
    ) == []
    assert short.is_open
