"""The position engine with an executor: the same exit rules, carried out through orders."""

from __future__ import annotations

from pathlib import Path

import pytest

from orb_intraday_bot.core.errors import EntryNotFilledError
from orb_intraday_bot.core.models import Direction
from orb_intraday_bot.paper.engine import PaperEngine
from orb_intraday_bot.paper.models import ExitReason, Position
from tests.execution_support import SYMBOL, Rig, ScriptedVenue, make_rig
from tests.helpers import at
from tests.unit.test_paper import CFG, LOT, OPENED, make_trade


class Hooks:
    def __init__(self) -> None:
        self.opened: list[Position] = []
        self.closed: list[Position] = []
        self.enabled = True


def _engine(rig: Rig, hooks: Hooks) -> PaperEngine:
    return PaperEngine(
        CFG, rig.store, rig.executor, executor_enabled=lambda: hooks.enabled,
        on_open=hooks.opened.append, on_close=hooks.closed.append,
    )


def test_open_through_the_executor_records_the_real_fill_and_calls_the_hook(
    tmp_path: Path,
) -> None:
    rig, hooks = make_rig(tmp_path), Hooks()
    engine = _engine(rig, hooks)
    trade, plan = make_trade()
    position = engine.open(trade, plan, OPENED)
    assert position is not None
    assert position.order_mode == "dry_run"
    assert position.entry_ltp == 150.2 and position.lots == 1
    assert position.entry_order_id.startswith("DRY-")
    assert hooks.opened == [position]
    saved = rig.store.load_position(position.position_id)
    assert saved is not None and saved.order_mode == "dry_run" and saved.stop_order_id


def test_a_one_lot_cap_leaves_nothing_to_book_at_t1(tmp_path: Path) -> None:
    rig, hooks = make_rig(tmp_path), Hooks()
    trade, plan = make_trade()
    assert plan.t1_book_lots == 3                           # six planned lots book three at T1
    position = _engine(rig, hooks).open(trade, plan, OPENED)
    assert position is not None and position.t1_book_lots == 0


def test_an_unfilled_entry_opens_nothing(tmp_path: Path) -> None:
    rig, hooks = make_rig(tmp_path, kind="live"), Hooks()
    venue = rig.venue
    assert isinstance(venue, ScriptedVenue)
    venue.script["ENTRY"] = [0.0]
    engine = _engine(rig, hooks)
    trade, plan = make_trade()
    with pytest.raises(EntryNotFilledError):
        engine.open(trade, plan, OPENED)
    assert engine.open_positions == [] and hooks.opened == []
    assert not engine.has_position(trade.signal.signal_id)


def test_the_hard_stop_sells_through_the_executor_at_the_bid(tmp_path: Path) -> None:
    rig, hooks = make_rig(tmp_path), Hooks()
    engine = _engine(rig, hooks)
    trade, plan = make_trade()
    position = engine.open(trade, plan, OPENED)
    assert position is not None
    stop_key = position.stop_order_key
    rig.quotes.set(135.0, 135.4, ltp=135.2)
    events = engine.on_tick(at(9, 45), 22760.0, {SYMBOL: 135.2})     # through the 22765 stop
    fill = events[0].fill
    assert fill is not None and fill.reason is ExitReason.HARD_STOP
    assert fill.price_source == "dry_run" and fill.option_price == 135.0
    assert not position.is_open and hooks.closed == [position]
    stop = rig.store.load_order(stop_key)
    assert stop is not None and stop.status.value == "CANCELLED"
    assert rig.venue.net_quantities() == {}                          # type: ignore[union-attr]
    assert position.net_option_pnl < 0                               # sold lower than bought


def test_a_broker_side_stop_that_fires_is_booked_as_one(tmp_path: Path) -> None:
    rig, hooks = make_rig(tmp_path), Hooks()
    engine = _engine(rig, hooks)
    trade, plan = make_trade()
    position = engine.open(trade, plan, OPENED)
    assert position is not None
    rig.quotes.set(129.9, 130.1, ltp=130.0)                          # the option fell through it
    events = engine.on_tick(at(9, 45), 22790.0, {SYMBOL: 130.0})     # futures still above stop
    fill = events[0].fill
    assert fill is not None and fill.reason is ExitReason.BROKER_STOP
    assert fill.option_price == 129.9 and fill.quantity == LOT
    assert not position.is_open and hooks.closed == [position]
    assert engine.open_positions == []


def test_t1_moves_the_broker_stop_to_cost(tmp_path: Path) -> None:
    rig, hooks = make_rig(tmp_path), Hooks()
    engine = _engine(rig, hooks)
    trade, plan = make_trade()
    position = engine.open(trade, plan, OPENED)
    assert position is not None
    old_key = position.stop_order_key
    rig.quotes.set(158.0, 158.4, ltp=158.2)
    engine.on_tick(at(9, 45), 22831.0, {SYMBOL: 158.2})              # T1 is 22830
    assert position.t1_done and position.stop_at_entry and position.is_open
    assert position.stop_trigger == pytest.approx(150.2)             # now at cost
    assert position.stop_order_key != old_key
    first = rig.store.load_order(old_key)
    assert first is not None and first.status.value == "CANCELLED"


def test_a_failed_sale_leaves_the_position_open_and_is_retried(tmp_path: Path) -> None:
    rig, hooks = make_rig(tmp_path, kind="live"), Hooks()
    venue = rig.venue
    assert isinstance(venue, ScriptedVenue)
    engine = _engine(rig, hooks)
    trade, plan = make_trade()
    position = engine.open(trade, plan, OPENED)
    assert position is not None
    venue.cancel_works = False                                       # the stop cannot be removed
    events = engine.on_tick(at(9, 45), 22760.0, {SYMBOL: 135.0})
    assert events == [] and position.is_open and hooks.closed == []
    venue.cancel_works = True                                        # and then it can
    events = engine.on_tick(at(9, 45), 22760.0, {SYMBOL: 135.0})
    assert len(events) == 1 and not position.is_open and hooks.closed == [position]


def test_square_off_closes_executed_positions_and_leaves_paper_ones(tmp_path: Path) -> None:
    rig, hooks = make_rig(tmp_path), Hooks()
    engine = _engine(rig, hooks)
    hooks.enabled = False
    paper_trade, paper_plan = make_trade(Direction.SHORT, "p1")
    paper = engine.open(paper_trade, paper_plan, OPENED)
    hooks.enabled = True
    trade, plan = make_trade()
    live = engine.open(trade, plan, OPENED)
    assert paper is not None and live is not None
    assert (paper.order_mode, live.order_mode) == ("paper", "dry_run")
    rig.quotes.set(149.0, 149.4, ltp=149.2)
    events = engine.close_all(
        at(15, 20), 22800.0, None, ExitReason.SQUARE_OFF, executed_only=True
    )
    assert [e.fill.reason for e in events if e.fill] == [ExitReason.SQUARE_OFF]
    assert not live.is_open and paper.is_open
    assert hooks.closed == [live]                                    # the hook ignores paper


def test_a_position_is_closed_the_way_it_was_opened(tmp_path: Path) -> None:
    rig, hooks = make_rig(tmp_path), Hooks()
    engine = _engine(rig, hooks)
    hooks.enabled = False
    trade, plan = make_trade()
    paper = engine.open(trade, plan, OPENED)
    assert paper is not None and paper.order_mode == "paper"
    hooks.enabled = True                                             # armed later in the day
    events = engine.on_tick(at(9, 45), 22760.0, None)
    fill = events[0].fill
    assert fill is not None and fill.price_source == "modeled"       # simulated, no order sent
    assert rig.store.orders_for_signal(paper.position_id) == []


def test_without_an_executor_nothing_changes() -> None:
    trade, plan = make_trade()
    position = PaperEngine(CFG).open(trade, plan, OPENED)
    assert position is not None
    assert position.order_mode == "paper" and position.entry_ltp == 150.0
    assert position.stop_order_id == ""


def test_the_candle_path_also_uses_the_executor(tmp_path: Path) -> None:
    from orb_intraday_bot.core.models import Candle

    rig, hooks = make_rig(tmp_path), Hooks()
    engine = _engine(rig, hooks)
    trade, plan = make_trade()
    position = engine.open(trade, plan, OPENED)
    assert position is not None
    rig.quotes.set(135.0, 135.4, ltp=135.2)
    candle = Candle(
        start=at(9, 36), open=22790.0, high=22795.0, low=22760.0, close=22790.0, volume=1000.0
    )
    events = engine.on_candle(candle, {SYMBOL: 135.2})
    fill = events[0].fill
    assert fill is not None and fill.price_source == "dry_run"
    assert not position.is_open
