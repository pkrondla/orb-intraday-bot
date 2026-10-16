"""The live executor: entry, protective stop, exits and reconciliation.

Most tests use the dry-run venue (a real simulation against bid/ask quotes); the awkward broker
behaviours (resting orders, partial fills, a cancel that does not work) use a scripted broker.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orb_intraday_bot.core.errors import EntryNotFilledError, ExitFailedError
from orb_intraday_bot.execution.dry_run_venue import DryRunVenue
from orb_intraday_bot.execution.models import OrderStatus
from orb_intraday_bot.paper.engine import PaperEngine
from orb_intraday_bot.paper.models import ExitReason, Position
from orb_intraday_bot.risk.limits import RiskLimits
from tests.execution_support import SYMBOL, Rig, ScriptedVenue, make_rig
from tests.helpers import at
from tests.unit.test_paper import CFG, LOT, OPENED, make_trade

NOW = at(9, 40)


def _enter(rig: Rig) -> Position:
    """Open a position through the engine, the way the live session does."""
    trade, plan = make_trade()
    engine = PaperEngine(CFG, rig.store, rig.executor)
    position = engine.open(trade, plan, OPENED)
    assert position is not None
    return position


def _status(rig: Rig, key: str) -> OrderStatus:
    order = rig.store.load_order(key)
    assert order is not None
    return order.status


# ---- entry -------------------------------------------------------------------------------
def test_entry_buys_at_the_ask_with_a_protective_limit(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    trade, plan = make_trade()
    done = rig.executor.enter(trade, plan, OPENED)
    assert done.price == 150.2                      # the ask, not the plan's 150.0
    assert done.lots == 1                           # capped from the plan's 6 by the risk limit
    assert done.order_id.startswith("DRY-ORB-")
    rows = rig.store.orders_for_signal(trade.signal.signal_id)
    assert [(r["leg"], r["side"], r["order_type"], r["mode"]) for r in rows] == [
        ("ENTRY", "BUY", "LIMIT", "dry_run")
    ]
    assert rows[0]["price"] == 153.25               # ask + 2%, rounded up to the 0.05 tick
    assert rows[0]["quantity"] == LOT


def test_entry_is_vetoed_without_a_quote(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.quotes.clear()
    trade, plan = make_trade()
    with pytest.raises(EntryNotFilledError, match="quote"):
        rig.executor.enter(trade, plan, OPENED)
    assert rig.store.orders_for_signal(trade.signal.signal_id) == []


def test_entry_is_vetoed_with_no_bid(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.quotes.set(None, 150.2)
    trade, plan = make_trade()
    with pytest.raises(EntryNotFilledError):
        rig.executor.enter(trade, plan, OPENED)


@pytest.mark.parametrize(
    ("bid", "ask", "reason"),
    [
        (140.0, 150.0, "spread"),          # about 7% wide
        (159.0, 160.0, "price sanity"),    # the ask ran 6.7% above the plan
    ],
)
def test_entry_is_vetoed_by_the_risk_gate(
    tmp_path: Path, bid: float, ask: float, reason: str
) -> None:
    rig = make_rig(tmp_path)
    rig.quotes.set(bid, ask)
    trade, plan = make_trade()
    with pytest.raises(EntryNotFilledError, match=reason):
        rig.executor.enter(trade, plan, OPENED)
    assert rig.store.orders_for_signal(trade.signal.signal_id) == []


def test_entry_is_vetoed_when_funds_are_short(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    assert not isinstance(rig.account, type(None))
    rig.account._funds.margin = 1_000.0                    # type: ignore[union-attr]
    trade, plan = make_trade()
    with pytest.raises(EntryNotFilledError, match="margin"):
        rig.executor.enter(trade, plan, OPENED)


def test_unknown_funds_do_not_block_the_entry(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.account._funds.margin = None                       # type: ignore[union-attr]
    trade, plan = make_trade()
    assert rig.executor.enter(trade, plan, OPENED).lots == 1


def test_entry_is_vetoed_once_the_risk_engine_has_tripped(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.risk.trip("daily loss limit")
    trade, plan = make_trade()
    with pytest.raises(EntryNotFilledError, match="halted"):
        rig.executor.enter(trade, plan, OPENED)


def test_a_resting_entry_is_cancelled_after_the_timeout_and_never_chased(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path, kind="live")
    venue = rig.venue
    assert isinstance(venue, ScriptedVenue)
    venue.script["ENTRY"] = [0.0]
    rig.quotes.set(149.8, 150.2)
    trade, plan = make_trade()
    with pytest.raises(EntryNotFilledError, match="not filled in time"):
        rig.executor.enter(trade, plan, OPENED)
    assert sum(rig.sleeps) == pytest.approx(rig.limits.entry_fill_timeout_sec)
    assert [o.status for o in venue.orders] == [OrderStatus.CANCELLED]
    assert len(venue.orders) == 1                          # no second try, no market fallback


def test_an_entry_that_fills_during_the_cancel_counts_as_filled(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, kind="live")
    venue = rig.venue
    assert isinstance(venue, ScriptedVenue)
    venue.script["ENTRY"] = [0.0]
    venue.fill_on_cancel = True
    venue.fill_price = 150.1
    trade, plan = make_trade()
    done = rig.executor.enter(trade, plan, OPENED)
    assert done.price == 150.1


def test_a_partly_filled_entry_is_sold_back_at_once(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, kind="live")
    venue = rig.venue
    assert isinstance(venue, ScriptedVenue)
    venue.script["ENTRY"] = [0.5]
    trade, plan = make_trade()
    with pytest.raises(EntryNotFilledError, match="partly filled"):
        rig.executor.enter(trade, plan, OPENED)
    sells = venue.of_leg("FLATTEN")
    assert len(sells) == 1
    assert sells[0].intent.side == "SELL" and sells[0].intent.quantity == LOT // 2
    assert sells[0].status is OrderStatus.FILLED


def test_a_partial_entry_that_cannot_be_sold_back_blocks_entries_and_alerts(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path, kind="live")
    venue = rig.venue
    assert isinstance(venue, ScriptedVenue)
    venue.script["ENTRY"] = [0.5]
    venue.script["FLATTEN"] = [0.0, 0.0, 0.0, 0.0]
    trade, plan = make_trade()
    with pytest.raises(EntryNotFilledError):
        rig.executor.enter(trade, plan, OPENED)
    assert rig.risk.state.entries_blocked
    assert any("could not be sold" in a for a in rig.alerts)


def test_an_entry_whose_cancel_is_unconfirmed_blocks_entries_and_alerts(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path, kind="live")
    venue = rig.venue
    assert isinstance(venue, ScriptedVenue)
    venue.script["ENTRY"] = [0.0]
    venue.cancel_works = False
    trade, plan = make_trade()
    with pytest.raises(EntryNotFilledError, match="could not be cancelled"):
        rig.executor.enter(trade, plan, OPENED)
    assert rig.risk.state.entries_blocked
    assert any("CRITICAL" in a for a in rig.alerts)


def test_a_rejected_entry_is_a_veto(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, kind="live")
    venue = rig.venue
    assert isinstance(venue, ScriptedVenue)
    original = venue.submit

    def reject(intent):  # type: ignore[no-untyped-def]
        record = original(intent)
        record.status = OrderStatus.REJECTED
        record.reject_reason = "RMS: margin"
        return record

    venue.submit = reject                                  # type: ignore[method-assign]
    trade, plan = make_trade()
    with pytest.raises(EntryNotFilledError, match="rejected by the broker"):
        rig.executor.enter(trade, plan, OPENED)


# ---- the protective stop -----------------------------------------------------------------
def test_open_places_a_protective_stop_below_the_entry(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    position = _enter(rig)
    assert position.order_mode == "dry_run" and position.lots == 1
    assert position.entry_ltp == 150.2 and position.planned_entry == 150.0
    assert position.sl_exit == pytest.approx(130.95)       # the plan's 130.75, shifted by the fill
    assert position.stop_order_id.startswith("DRY-ORB-")
    assert position.stop_trigger == pytest.approx(130.95)
    assert position.stop_quantity == LOT
    stop = rig.store.load_order(position.stop_order_key)
    assert stop is not None
    assert stop.intent.order_type == "SL" and stop.intent.side == "SELL"
    assert stop.intent.trigger_price == pytest.approx(130.95)
    assert 0 < (stop.intent.price or 0) < 130.95            # the limit sits under the trigger


def test_protect_is_a_no_op_when_the_stop_already_matches(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    position = _enter(rig)
    before = len(rig.store.orders_for_signal(position.position_id))
    rig.executor.protect(position, NOW)
    assert len(rig.store.orders_for_signal(position.position_id)) == before


def test_protect_replaces_the_stop_when_the_trigger_moves(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    position = _enter(rig)
    old_key = position.stop_order_key
    position.stop_at_entry = True                          # what T1 does: stop moves to cost
    rig.executor.protect(position, NOW)
    assert position.stop_trigger == pytest.approx(150.2)
    assert position.stop_order_key != old_key
    assert _status(rig, old_key) is OrderStatus.CANCELLED


def test_protect_follows_a_smaller_quantity_after_a_partial_exit(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    position = _enter(rig)
    position.lots = position.remaining_lots = 2            # as if 2 lots were held
    rig.executor.protect(position, NOW)
    assert position.stop_quantity == 2 * LOT
    position.remaining_lots = 1                            # one lot booked at T1
    rig.executor.protect(position, NOW)
    assert position.stop_quantity == LOT


def test_protect_ignores_positions_of_another_mode(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    position = _enter(rig)
    position.order_mode = "paper"
    position.stop_order_id, position.stop_order_key = "", ""
    rig.executor.protect(position, NOW)
    assert position.stop_order_id == ""


def test_a_rejected_stop_is_alerted_and_retried_later(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, kind="live")
    venue = rig.venue
    assert isinstance(venue, ScriptedVenue)
    original = venue.submit

    def reject_stops(intent):  # type: ignore[no-untyped-def]
        record = original(intent)
        if intent.order_type == "SL":
            record.status = OrderStatus.REJECTED
            record.reject_reason = "trigger above LTP"
        return record

    venue.submit = reject_stops                            # type: ignore[method-assign]
    rig.quotes.set(149.8, 150.2)
    position = _enter(rig)
    assert position.stop_order_id == ""                    # no stop, but the position is open
    assert any("protective stop" in a for a in rig.alerts)
    count = len(rig.alerts)
    rig.executor.protect(position, NOW)                    # tried again, alert is rate limited
    assert len(rig.alerts) == count


def test_a_stop_that_fires_is_reported_once_with_its_fill(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    position = _enter(rig)
    assert rig.executor.poll_stops([position], NOW) == []
    rig.quotes.set(129.9, 130.1, ltp=130.0)                # last price falls through the trigger
    fills = rig.executor.poll_stops([position], NOW)
    assert len(fills) == 1
    assert fills[0].price == 129.9 and fills[0].quantity == LOT
    assert fills[0].position_id == position.position_id


def test_a_stop_that_was_cancelled_at_the_broker_is_noticed_and_placed_again(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path)
    position = _enter(rig)
    stop = rig.store.load_order(position.stop_order_key)
    assert stop is not None
    rig.executor._stops[position.position_id].status = OrderStatus.CANCELLED
    assert rig.executor.poll_stops([position], NOW) == []
    assert position.stop_order_id == ""
    assert any("no longer active" in a for a in rig.alerts)
    rig.executor.protect(position, NOW)
    assert position.stop_order_id != ""


# ---- exits -------------------------------------------------------------------------------
def test_sell_cancels_the_stop_first_then_sells_at_the_bid(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    position = _enter(rig)
    stop_key = position.stop_order_key
    rig.quotes.set(140.0, 140.3)
    done = rig.executor.sell(position, 1, "OPTION_STOP", NOW)
    assert done.price == 140.0 and done.quantity == LOT and done.source == "dry_run"
    assert done.reason is None
    assert _status(rig, stop_key) is OrderStatus.CANCELLED
    assert position.unbooked_qty == 0
    assert rig.venue.net_quantities() == {}                # type: ignore[union-attr]


def test_sell_reports_the_broker_stop_if_it_already_executed(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    position = _enter(rig)
    rig.quotes.set(129.9, 130.1, ltp=130.0)
    done = rig.executor.sell(position, 1, "HARD_STOP", NOW)
    assert done.reason == ExitReason.BROKER_STOP.value
    assert done.price == 129.9
    venue = rig.venue
    assert isinstance(venue, DryRunVenue)
    assert venue.net_quantities() == {}                    # sold once, by the stop only


def test_sell_never_goes_ahead_if_the_stop_cannot_be_cancelled(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, kind="live")
    venue = rig.venue
    assert isinstance(venue, ScriptedVenue)
    rig.quotes.set(149.8, 150.2)
    position = _enter(rig)
    venue.cancel_works = False
    with pytest.raises(ExitFailedError, match="could not be cancelled"):
        rig.executor.sell(position, 1, "OPTION_STOP", NOW)
    assert venue.of_leg("EXIT") == []                      # nothing was sold


def test_sell_never_goes_ahead_if_the_stop_is_not_on_record(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    position = _enter(rig)
    position.stop_order_key = "gone"
    rig.executor._stops.clear()
    with pytest.raises(ExitFailedError, match="not selling blind"):
        rig.executor.sell(position, 1, "OPTION_STOP", NOW)


def test_sell_without_a_quote_uses_a_market_order(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, kind="live")
    venue = rig.venue
    assert isinstance(venue, ScriptedVenue)
    position = _enter(rig)
    rig.quotes.clear()
    done = rig.executor.sell(position, 1, "EOD", NOW)
    assert done.quantity == LOT
    assert [o.intent.order_type for o in venue.of_leg("EXIT")] == ["MARKET"]


def test_sell_falls_back_to_market_when_the_limit_rests(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, kind="live")
    venue = rig.venue
    assert isinstance(venue, ScriptedVenue)
    position = _enter(rig)
    venue.script["EXIT"] = [0.0]                           # the limit sell does not fill
    done = rig.executor.sell(position, 1, "EOD", NOW)
    exits = venue.of_leg("EXIT")
    assert [o.intent.order_type for o in exits] == ["LIMIT", "MARKET"]
    assert exits[0].status is OrderStatus.CANCELLED
    assert done.quantity == LOT


def test_a_partial_sale_is_remembered_and_only_the_rest_is_sold_on_retry(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path, kind="live")
    venue = rig.venue
    assert isinstance(venue, ScriptedVenue)
    position = _enter(rig)
    venue.script["EXIT"] = [0.5, 0.0, 0.0, 0.0]            # half sold, then nothing fills
    with pytest.raises(ExitFailedError):
        rig.executor.sell(position, 1, "EOD", NOW)
    sold = LOT // 2
    assert position.unbooked_qty == sold
    assert position.unbooked_value > 0
    assert position.stop_quantity == LOT - sold            # the safety stop covers what is left
    venue.script["EXIT"] = []                              # the market recovers
    done = rig.executor.sell(position, 1, "EOD", NOW)
    assert done.quantity == LOT                            # the whole position, booked once
    assert position.unbooked_qty == 0 and position.unbooked_value == 0
    total_sold = sum(o.filled_qty for o in venue.of_leg("EXIT"))
    assert total_sold == LOT                               # nothing was sold twice


def test_a_failed_sale_puts_the_safety_stop_back_and_alerts(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, kind="live")
    venue = rig.venue
    assert isinstance(venue, ScriptedVenue)
    position = _enter(rig)
    venue.script["EXIT"] = [0.0, 0.0, 0.0, 0.0]
    with pytest.raises(ExitFailedError):
        rig.executor.sell(position, 1, "EOD", NOW)
    assert position.stop_order_id != ""                    # protected again
    assert any("incomplete" in a for a in rig.alerts)


def test_sell_from_another_mode_is_refused(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    position = _enter(rig)
    position.order_mode = "live"
    with pytest.raises(ExitFailedError, match="another order mode"):
        rig.executor.sell(position, 1, "EOD", NOW)


# ---- reconciliation ----------------------------------------------------------------------
def test_reconcile_is_clean_when_the_books_agree(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    position = _enter(rig)
    assert rig.executor.reconcile([position], NOW) == []
    assert not rig.risk.state.entries_blocked


def test_reconcile_flags_a_dry_run_ledger_that_differs(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    position = _enter(rig)
    position.remaining_lots = 2                            # the bot thinks it holds more
    problems = rig.executor.reconcile([position], NOW)
    assert problems and "holds" in problems[0]
    assert rig.risk.state.entries_blocked
    assert not rig.risk.state.halted                       # never a flatten: nothing is sold
    assert any("CRITICAL" in a for a in rig.alerts)


def test_reconcile_flags_a_symbol_the_dry_run_ledger_holds_unexpectedly(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path)
    position = _enter(rig)
    assert isinstance(rig.venue, DryRunVenue)
    rig.venue._net["OTHER"] = 75
    problems = rig.executor.reconcile([position], NOW)
    assert any("OTHER" in p for p in problems)


def test_live_reconcile_flags_a_shortfall_but_not_extra_shares(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, kind="live")
    position = _enter(rig)
    account = rig.account
    account.positions = {SYMBOL: LOT, "GROWWBOT": 150}      # type: ignore[union-attr]
    assert rig.executor.reconcile([position], NOW) == []    # another bot's shares are not ours
    account.positions = {SYMBOL: 0}                         # type: ignore[union-attr]
    assert rig.executor.reconcile([position], NOW)


def test_an_unreadable_broker_is_not_a_mismatch(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, kind="live")
    position = _enter(rig)
    rig.account.positions = None                            # type: ignore[union-attr]
    assert rig.executor.reconcile([position], NOW) == []
    assert not rig.risk.state.entries_blocked


def test_a_filled_entry_with_no_position_on_record_is_found(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    trade, plan = make_trade()
    rig.executor.enter(trade, plan, OPENED)                 # filled, but never saved as a position
    problems = rig.executor.reconcile([], OPENED)
    assert any("no position is on record" in p for p in problems)


def test_reconcile_runs_at_most_once_per_interval(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, kind="live")
    position = _enter(rig)
    calls: list[int] = []
    account = rig.account
    original = account.net_quantities
    account.net_quantities = lambda: calls.append(1) or original()   # type: ignore[union-attr,method-assign]
    rig.executor.maybe_reconcile([position], NOW)
    rig.executor.maybe_reconcile([position], NOW.replace(second=30))
    assert len(calls) == 1
    rig.executor.maybe_reconcile([position], at(9, 42))
    assert len(calls) == 2


def test_startup_rebuilds_the_dry_run_ledger_from_open_positions(tmp_path: Path) -> None:
    first = make_rig(tmp_path)
    position = _enter(first)
    # A new process: a fresh executor and an empty simulated ledger, the same database.
    second = make_rig(tmp_path)
    problems = second.executor.startup([position], NOW)
    assert problems == []
    assert isinstance(second.venue, DryRunVenue)
    assert second.venue.net_quantities() == {SYMBOL: LOT}
    assert position.stop_order_id                           # the stop is still the old one
    second.quotes.set(140.0, 140.3)
    done = second.executor.sell(position, 1, "EOD", NOW)    # and it can be sold after a restart
    assert done.price == 140.0


def test_startup_places_a_missing_protective_stop(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, kind="live")
    position = _enter(rig)
    position.stop_order_id = position.stop_order_key = ""
    position.stop_trigger, position.stop_quantity = 0.0, 0
    rig.account.positions = {SYMBOL: LOT}                   # type: ignore[union-attr]
    rig.executor.startup([position], NOW)
    assert position.stop_order_id != ""


# ---- misc --------------------------------------------------------------------------------
def test_attempt_numbers_continue_after_a_restart(tmp_path: Path) -> None:
    first = make_rig(tmp_path)
    trade, plan = make_trade()
    first.executor.enter(trade, plan, OPENED)
    second = make_rig(tmp_path)
    second.risk.state.open_positions = 0
    second.executor.enter(trade, plan, OPENED)
    keys = [r["idempotency_key"] for r in second.store.orders_for_signal(trade.signal.signal_id)]
    assert len(keys) == len(set(keys)) == 2


def test_alerts_are_rate_limited(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    for _ in range(5):
        rig.executor._alert("same", "something")
    assert rig.alerts == ["something"]
    rig.clock.advance(seconds=301)
    rig.executor._alert("same", "something")
    assert len(rig.alerts) == 2


def test_a_failing_alert_channel_never_breaks_trading(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)

    def broken(_: str) -> None:
        raise OSError("telegram down")

    rig.executor._notify = broken
    rig.executor._alert("k", "text")                        # must not raise


def test_a_database_error_does_not_stop_an_order_that_is_already_placed(
    tmp_path: Path,
) -> None:
    rig = make_rig(tmp_path)
    rig.store.close()                                       # every later write now fails
    trade, plan = make_trade()
    assert rig.executor.enter(trade, plan, OPENED).lots == 1


def test_limits_are_the_documented_defaults() -> None:
    limits = RiskLimits()
    assert (limits.entry_fill_timeout_sec, limits.exit_fill_timeout_sec) == (10.0, 6.0)
