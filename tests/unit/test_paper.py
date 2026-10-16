from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from orb_intraday_bot.config.settings import ChargesConfig, StrategyConfig
from orb_intraday_bot.core.models import Candle, Direction, Zone
from orb_intraday_bot.core.signals import Setup, SetupSignal
from orb_intraday_bot.engine.signal_engine import PlannedTrade, SignalEngine
from orb_intraday_bot.options.chain import OptionContract, OptionKind, OptionQuote
from orb_intraday_bot.options.pricing import OptionPlan, plan_option_trade
from orb_intraday_bot.options.selector import OptionSelection
from orb_intraday_bot.paper.charges import order_charges
from orb_intraday_bot.paper.engine import PaperEngine
from orb_intraday_bot.paper.exits import Bar, evaluate_bar, modeled_option_price
from orb_intraday_bot.paper.models import ExitReason, Position
from orb_intraday_bot.paper.report import (
    format_closed_position,
    format_open_position,
    format_summary,
    summarize,
)
from orb_intraday_bot.risk.stops import StopPlan
from orb_intraday_bot.risk.targets import TargetPlan
from orb_intraday_bot.storage.store import TradeStore
from tests.helpers import DAY, at

CFG = StrategyConfig()
LOT = 65
OPENED = at(9, 36)          # close of the signal candle (09:33)


def make_trade(
    direction: Direction = Direction.LONG, sign: str = "s1"
) -> tuple[PlannedTrade, OptionPlan]:
    s = 1 if direction is Direction.LONG else -1
    entry = 22800.0
    kind = OptionKind.CE if s > 0 else OptionKind.PE
    signal = SetupSignal(
        setup=Setup.ORB_BREAKOUT, direction=direction, level_name="ORH" if s > 0 else "ORL",
        level_value=entry - s * 20, candle_start=at(9, 33), entry_price=entry,
        entry_basis="previous candle low", sign_id=sign, stop_level=entry - s * 20,
        stop_on="close", target_ref="R2" if s > 0 else "S2",
    )
    stop = StopPlan(20.0, entry - s * 35, entry - s * 20, "close level is 20.0 pts away")
    targets = TargetPlan(entry + s * 30, entry + s * 100, 30, 100, "nearest pivot", "R2")
    quote = OptionQuote(22800, kind, f"NIFTY26O19{kind.value}", 150.0, 0.55)
    selection = OptionSelection(quote, 22800, "atm", (), (("atm", quote),))
    contract = OptionContract(quote.trading_symbol, "g", date(2026, 10, 19), 22800, kind, LOT, 0.05)
    plan = plan_option_trade(
        direction=direction, futures_entry=entry, stop=stop, targets=targets,
        selection=selection, contract=contract, config=CFG,
    )
    return PlannedTrade(signal, Zone.NEUTRAL, stop, targets, plan.risk_budget_inr), plan


def opened(direction: Direction = Direction.LONG, config: StrategyConfig = CFG) -> Position:
    trade, plan = make_trade(direction)
    position = PaperEngine(config).open(trade, plan, OPENED)
    assert position is not None
    return position


def bar(hh: int, mm: int, o: float, h: float, low: float, c: float, candle: bool = True) -> Bar:
    return Bar(at(hh, mm), o, h, low, c, is_candle=candle)


def reasons(position: Position) -> list[ExitReason]:
    return [f.reason for f in position.fills]


# ---- charges -------------------------------------------------------------------------------
def test_buy_side_charges_have_stamp_duty_and_no_stt() -> None:
    c = order_charges("BUY", 150.0, 260, ChargesConfig())
    turnover = 150.0 * 260
    assert c.stt == 0 and c.stamp == pytest.approx(turnover * 0.00003)
    assert c.brokerage == 20.0                       # flat 20 is below 0.1% of 39,000
    assert c.gst == pytest.approx(0.18 * (c.brokerage + c.exchange + c.sebi))
    assert c.total == round(c.brokerage + c.exchange + c.sebi + c.stamp + c.gst, 2)


def test_sell_side_charges_have_stt_and_no_stamp() -> None:
    c = order_charges("SELL", 150.0, 260, ChargesConfig())
    assert c.stamp == 0 and c.stt == pytest.approx(150.0 * 260 * 0.0015)


def test_brokerage_is_capped_by_percentage_on_small_orders() -> None:
    c = order_charges("BUY", 10.0, 65, ChargesConfig())     # turnover 650 -> 0.1% = 0.65
    assert c.brokerage == pytest.approx(0.65)


def test_no_charges_without_turnover() -> None:
    assert order_charges("SELL", 0.0, 100, ChargesConfig()).total == 0
    assert order_charges("SELL", 100.0, 0, ChargesConfig()).total == 0


# ---- opening -------------------------------------------------------------------------------
def test_open_copies_the_plan_and_books_entry_charges() -> None:
    p = opened()
    assert (p.lots, p.lot_size, p.remaining_lots, p.t1_book_lots) == (6, LOT, 6, 3)
    assert (p.entry_ltp, p.hard_stop, p.close_exit_level) == (150.0, 22765.0, 22780.0)
    assert (p.t1, p.t2, p.futures_entry) == (22830.0, 22900.0, 22800.0)
    assert p.sl_exit == 130.75                      # 35 points x 0.55 below entry, not 20 x 0.55
    assert p.entry_charges == order_charges("BUY", 150.0, 6 * LOT, CFG.paper.charges).total
    assert p.is_open and p.opened_at == OPENED


def test_slippage_raises_entry_and_lowers_exits() -> None:
    cfg = StrategyConfig.model_validate({"paper": {"slippage_option_points": 0.5}})
    p = opened(config=cfg)
    assert p.entry_ltp == 150.5
    evaluate_bar(p, bar(9, 36, 22800, 22800, 22760, 22790), None, cfg.paper)
    assert p.fills[0].option_price == pytest.approx(150.5 - 35 * 0.55 - 0.5)


# ---- stops ---------------------------------------------------------------------------------
def test_hard_stop_on_touch_fills_at_the_stop_price() -> None:
    p = opened()
    events = evaluate_bar(p, bar(9, 36, 22790, 22795, 22760, 22790), None, CFG.paper)
    fill = events[0].fill
    assert fill is not None and fill.reason is ExitReason.HARD_STOP
    assert (fill.futures_price, fill.option_price) == (22765.0, 130.75)
    assert fill.price_source == "modeled"
    assert fill.lots == 6 and not p.is_open and p.closed_at == at(9, 36)
    assert fill.futures_points == -35.0
    assert fill.option_gross == pytest.approx((130.75 - 150.0) * 6 * LOT)


def test_gap_through_the_stop_fills_at_the_open() -> None:
    p = opened()
    evaluate_bar(p, bar(9, 36, 22750, 22755, 22740, 22745), None, CFG.paper)
    assert p.fills[0].futures_price == 22750.0
    assert p.fills[0].option_price == modeled_option_price(opened(), 22750.0)


def test_stop_beats_target_when_one_candle_reaches_both() -> None:
    p = opened()
    evaluate_bar(p, bar(9, 36, 22800, 22835, 22760, 22800), None, CFG.paper)
    assert reasons(p) == [ExitReason.HARD_STOP]


def test_close_below_the_level_exits_at_the_close_without_touching_the_hard_stop() -> None:
    p = opened()
    events = evaluate_bar(p, bar(9, 36, 22795, 22798, 22772, 22775), None, CFG.paper)
    fill = events[0].fill
    assert fill is not None and fill.reason is ExitReason.CLOSE_STOP
    assert fill.futures_price == 22775.0
    assert fill.option_price == pytest.approx(150 - 25 * 0.55, abs=0.03)


def test_close_exactly_on_the_level_is_not_a_failure() -> None:
    p = opened()
    assert evaluate_bar(p, bar(9, 36, 22795, 22798, 22778, 22780), None, CFG.paper) == []


def test_a_live_poll_below_the_level_does_not_trigger_the_close_stop() -> None:
    p = opened()
    assert evaluate_bar(p, Bar.tick(at(9, 37), 22775.0), None, CFG.paper) == []


def test_option_stop_uses_the_live_premium() -> None:
    p = opened()
    # Futures still above the stop, but the premium fell through the option stop.
    events = evaluate_bar(p, Bar.tick(at(9, 37), 22790.0), 130.0, CFG.paper)
    fill = events[0].fill
    assert fill is not None and fill.reason is ExitReason.OPTION_STOP
    assert (fill.option_price, fill.price_source) == (130.0, "live")


def test_live_poll_fills_at_the_live_premium() -> None:
    p = opened()
    events = evaluate_bar(p, Bar.tick(at(9, 37), 22764.0), 131.4, CFG.paper)
    fill = events[0].fill
    assert fill is not None and fill.reason is ExitReason.HARD_STOP
    assert (fill.option_price, fill.price_source) == (131.4, "live")


def test_a_candle_touch_is_modelled_even_if_a_premium_is_supplied() -> None:
    # The premium given with a closed candle is the price now, not at the touch.
    p = opened()
    evaluate_bar(p, bar(9, 36, 22790, 22795, 22760, 22790), 140.0, CFG.paper)
    assert p.fills[0].price_source == "modeled" and p.fills[0].option_price == 130.75


def test_bars_before_the_position_opened_are_ignored() -> None:
    p = opened()
    assert evaluate_bar(p, bar(9, 33, 22790, 22795, 22700, 22700), None, CFG.paper) == []
    assert p.is_open


# ---- targets -------------------------------------------------------------------------------
def test_t1_books_part_and_moves_the_stop_to_entry() -> None:
    p = opened()
    events = evaluate_bar(p, bar(9, 36, 22800, 22832, 22798, 22830), None, CFG.paper)
    fill = events[0].fill
    assert fill is not None and fill.reason is ExitReason.T1 and fill.lots == 3
    assert fill.option_price == pytest.approx(150 + 30 * 0.55, abs=0.03)
    assert events[1].fill is None and "stop moved to entry" in events[1].message
    assert p.remaining_lots == 3 and p.t1_done and p.stop_at_entry and p.active_stop == 22800.0
    assert p.is_open


def test_after_t1_a_return_to_entry_exits_at_cost() -> None:
    p = opened()
    evaluate_bar(p, bar(9, 36, 22800, 22832, 22798, 22830), None, CFG.paper)
    evaluate_bar(p, bar(9, 39, 22830, 22831, 22799, 22805), None, CFG.paper)
    last = p.fills[-1]
    assert last.reason is ExitReason.BREAKEVEN_STOP and last.lots == 3
    assert (last.futures_price, last.option_price, last.option_gross) == (22800.0, 150.0, 0.0)
    assert not p.is_open


def test_break_even_stop_waits_for_the_next_bar() -> None:
    p = opened()
    # The same candle hit T1 and came back to entry: T1 is booked, the position stays open.
    evaluate_bar(p, bar(9, 36, 22800, 22832, 22795, 22810), None, CFG.paper)
    assert reasons(p) == [ExitReason.T1] and p.is_open


def test_t1_and_t2_in_one_candle() -> None:
    p = opened()
    evaluate_bar(p, bar(9, 36, 22800, 22905, 22799, 22900), None, CFG.paper)
    assert reasons(p) == [ExitReason.T1, ExitReason.T2] and not p.is_open
    assert p.fills[1].option_price == pytest.approx(150 + 100 * 0.55, abs=0.03)


def test_gap_above_the_target_fills_at_the_open() -> None:
    p = opened()
    evaluate_bar(p, bar(9, 36, 22910, 22920, 22905, 22915), None, CFG.paper)
    assert [f.futures_price for f in p.fills] == [22910.0, 22910.0]


def test_single_lot_has_no_partial_booking() -> None:
    cfg = StrategyConfig.model_validate({"sizing": {"capital_inr": 100_000}})
    trade, plan = make_trade()
    plan1 = plan_option_trade(
        direction=Direction.LONG, futures_entry=22800.0, stop=trade.stop, targets=trade.targets,
        selection=OptionSelection(
            OptionQuote(22800, OptionKind.CE, "X", 150.0, 0.55), 22800, "atm", (), ()
        ),
        contract=plan.contract, config=StrategyConfig.model_validate(
            {"sizing": {"capital_inr": 100_000, "risk_pct_per_trade": 0.0125}}
        ),
    )
    assert plan1.lots == 1 and plan1.t1_book_lots == 0
    p = PaperEngine(cfg).open(trade, plan1, OPENED)
    assert p is not None
    events = evaluate_bar(p, bar(9, 36, 22800, 22832, 22798, 22830), None, cfg.paper)
    assert [e.fill for e in events] == [None, ]  # one state-change event, no order
    assert p.remaining_lots == 1 and p.stop_at_entry


def test_short_trade_mirrors_stops_and_targets() -> None:
    p = opened(Direction.SHORT)
    assert (p.hard_stop, p.close_exit_level, p.t1, p.t2) == (22835.0, 22820.0, 22770.0, 22700.0)
    evaluate_bar(p, bar(9, 36, 22800, 22802, 22768, 22770), None, CFG.paper)
    assert reasons(p) == [ExitReason.T1]
    assert p.fills[0].option_price == pytest.approx(150 + 30 * 0.55, abs=0.03)
    evaluate_bar(p, bar(9, 39, 22770, 22801, 22765, 22790), None, CFG.paper)
    assert reasons(p) == [ExitReason.T1, ExitReason.BREAKEVEN_STOP]


def test_short_close_stop_needs_a_close_above_the_level() -> None:
    p = opened(Direction.SHORT)
    assert evaluate_bar(p, bar(9, 36, 22805, 22820, 22800, 22820), None, CFG.paper) == []
    events = evaluate_bar(p, bar(9, 39, 22815, 22825, 22812, 22822), None, CFG.paper)
    assert events[0].fill is not None and events[0].fill.reason is ExitReason.CLOSE_STOP


# ---- P&L -----------------------------------------------------------------------------------
def test_full_trade_pnl_both_ways() -> None:
    p = opened()
    evaluate_bar(p, bar(9, 36, 22800, 22832, 22798, 22830), None, CFG.paper)      # T1: 3 lots
    evaluate_bar(p, bar(9, 39, 22830, 22905, 22829, 22900), None, CFG.paper)      # T2: 3 lots
    t1_price, t2_price = p.fills[0].option_price, p.fills[1].option_price
    gross = (t1_price - 150) * 3 * LOT + (t2_price - 150) * 3 * LOT
    assert p.gross_option_pnl == pytest.approx(gross)
    assert p.total_charges == pytest.approx(
        p.entry_charges + p.fills[0].charges + p.fills[1].charges
    )
    assert p.net_option_pnl == pytest.approx(gross - p.total_charges, abs=0.01)
    assert p.futures_points == pytest.approx((30 + 100) / 2)                       # lot-weighted
    assert p.net_option_pnl < p.gross_option_pnl


def test_unrealized_pnl_uses_remaining_lots() -> None:
    p = opened()
    evaluate_bar(p, bar(9, 36, 22800, 22832, 22798, 22830), None, CFG.paper)
    assert p.unrealized(170.0) == pytest.approx(20 * 3 * LOT)


# ---- paper engine --------------------------------------------------------------------------
def candle(hh: int, mm: int, o: float, h: float, low: float, c: float) -> Candle:
    return Candle(at(hh, mm), o, h, low, c)


def test_open_is_idempotent() -> None:
    engine = PaperEngine(CFG)
    trade, plan = make_trade()
    assert engine.open(trade, plan, OPENED) is not None
    assert engine.open(trade, plan, OPENED) is None
    assert len(engine.open_positions) == 1
    assert engine.open_directions() == [Direction.LONG]


def test_engine_runs_exits_and_archives_closed_positions() -> None:
    engine = PaperEngine(CFG)
    trade, plan = make_trade()
    engine.open(trade, plan, OPENED)
    assert engine.on_candle(candle(9, 36, 22800, 22810, 22790, 22805)) == []
    events = engine.on_candle(candle(9, 39, 22805, 22806, 22760, 22770))
    assert [e.fill.reason for e in events if e.fill] == [ExitReason.HARD_STOP]
    assert engine.open_positions == [] and len(engine.closed_positions) == 1
    assert engine.open(trade, plan, at(9, 42)) is None          # same signal cannot reopen


def test_end_of_day_closes_open_positions_at_the_last_close() -> None:
    engine = PaperEngine(CFG)
    trade, plan = make_trade()
    engine.open(trade, plan, OPENED)
    engine.on_candle(candle(15, 24, 22800, 22810, 22795, 22805))
    assert len(engine.open_positions) == 1
    events = engine.on_candle(candle(15, 27, 22805, 22812, 22800, 22808))
    fill = events[-1].fill
    assert fill is not None and fill.reason is ExitReason.EOD
    assert fill.time == at(15, 30) and fill.futures_price == 22808.0
    assert engine.open_positions == []


def test_close_all_for_a_shutdown_at_the_session_end() -> None:
    engine = PaperEngine(CFG)
    trade, plan = make_trade()
    engine.open(trade, plan, OPENED)
    events = engine.close_all(at(15, 30), 22790.0, {plan.contract.trading_symbol: 146.0})
    fill = events[0].fill
    assert fill is not None and (fill.reason, fill.option_price) == (ExitReason.EOD, 146.0)


def test_mark_to_market_prefers_live_premium() -> None:
    engine = PaperEngine(CFG)
    trade, plan = make_trade()
    engine.open(trade, plan, OPENED)
    (position, modeled), = engine.mark_to_market(22820.0)
    assert modeled == pytest.approx(150 + 20 * 0.55, abs=0.03)
    (_, live), = engine.mark_to_market(22820.0, {position.trading_symbol: 163.0})
    assert live == 163.0
    text = format_open_position(position, 22820.0, live)
    assert "+20.0 pts" in text and "6/6 lot(s)" in text


# ---- storage -------------------------------------------------------------------------------
def test_position_round_trips_through_json() -> None:
    p = opened()
    evaluate_bar(p, bar(9, 36, 22800, 22832, 22798, 22830), None, CFG.paper)
    assert Position.from_dict(p.to_dict()) == p


def test_signals_are_recorded_once_and_can_change_status() -> None:
    store = TradeStore(":memory:")
    trade, _ = make_trade()
    sig = trade.signal
    assert store.record_signal(sig, "accepted") is True
    assert store.record_signal(sig, "accepted") is False
    store.set_signal_status(sig.signal_id, "released", "option", "no lot fits")
    assert store.signal_status(sig.signal_id) == "released"
    rows = store.signals_for_day(DAY)
    assert [(r["setup"], r["status"], r["reason"]) for r in rows] == [
        ("ORB_BREAKOUT", "released", "no lot fits")
    ]


def test_store_saves_fills_and_restores_open_positions(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "paper.sqlite"
    store = TradeStore(db)
    engine = PaperEngine(CFG, store)
    trade, plan = make_trade()
    position = engine.open(trade, plan, OPENED)
    assert position is not None
    engine.on_candle(candle(9, 36, 22800, 22832, 22798, 22830))       # T1: partial, stays open
    rows = store.fills_for_position(position.position_id)
    assert [(r["reason"], r["lots"]) for r in rows] == [("T1", 3)]
    store.close()

    # A restart: the open position comes back with its state and cannot be opened again.
    store2 = TradeStore(db)
    engine2 = PaperEngine(CFG, store2)
    (restored,) = engine2.open_positions
    assert restored.remaining_lots == 3 and restored.stop_at_entry and restored.t1_done
    assert restored.opened_at.utcoffset() == timedelta(hours=5, minutes=30)
    assert engine2.open(trade, plan, OPENED) is None
    engine2.on_candle(candle(9, 39, 22830, 22831, 22799, 22805))       # break-even exit
    assert engine2.open_positions == []
    assert [r["reason"] for r in store2.fills_for_position(position.position_id)] == [
        "T1", "BREAKEVEN_STOP"
    ]
    assert store2.open_positions() == []
    store2.close()


def test_saving_twice_does_not_duplicate_fills() -> None:
    store = TradeStore(":memory:")
    engine = PaperEngine(CFG, store)
    trade, plan = make_trade()
    position = engine.open(trade, plan, OPENED)
    assert position is not None
    engine.on_candle(candle(9, 36, 22800, 22832, 22798, 22830))
    store.save_position(position, at(9, 40))
    assert len(store.fills_for_position(position.position_id)) == 1


def test_day_summary_counts_wins_losses_and_costs() -> None:
    store = TradeStore(":memory:")
    engine = PaperEngine(CFG, store)
    win, win_plan = make_trade(sign="win")
    loss, loss_plan = make_trade(Direction.SHORT, sign="loss")
    engine.open(win, win_plan, OPENED)
    engine.open(loss, loss_plan, at(9, 48))
    engine.on_candle(candle(9, 48, 22800, 22905, 22790, 22900))   # long wins, short is stopped
    summary = store.day_summary(DAY)
    assert summary.trades == 2 and summary.open_trades == 0
    assert (summary.wins, summary.losses) == (1, 1)
    assert summary.net_option_pnl == pytest.approx(summary.gross_option_pnl - summary.charges)
    assert "1 win / 1 loss" in format_summary(summary)
    assert summary == summarize(DAY, store.positions_for_day(DAY))


def test_format_closed_position() -> None:
    p = opened()
    evaluate_bar(p, bar(9, 36, 22790, 22795, 22760, 22790), None, CFG.paper)
    text = format_closed_position(p)
    assert "HARD_STOPx6" in text and "net Rs -" in text and "-35.0 pts" in text


# ---- signal engine release -----------------------------------------------------------------
def test_releasing_an_unopened_trade_frees_its_sign_and_counters() -> None:
    engine = SignalEngine(CFG, None)
    trade, _ = make_trade()
    engine.taken_signs.add(trade.signal.sign_id)
    engine.trades_today = 1
    engine.release(trade)
    assert trade.signal.sign_id not in engine.taken_signs and engine.trades_today == 0
    engine.release(trade)                 # releasing twice does nothing
    assert engine.trades_today == 0
