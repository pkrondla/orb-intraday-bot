"""The dry-run venue: a simulated broker that fills against real bid/ask quotes."""

from __future__ import annotations

from orb_intraday_bot.core.clock import FixedClock
from orb_intraday_bot.execution.dry_run_venue import DryRunVenue
from orb_intraday_bot.execution.models import OrderIntent, OrderLeg, OrderStatus
from tests.execution_support import SYMBOL, Quotes
from tests.helpers import at


def _rig() -> tuple[DryRunVenue, Quotes]:
    quotes = Quotes()
    quotes.set(99.0, 100.0)
    return DryRunVenue(quotes, FixedClock(at(9, 40))), quotes


def _order(
    side: str = "BUY", order_type: str = "MARKET", qty: int = 75, price: float | None = None,
    trigger: float | None = None, attempt: int = 1, leg: OrderLeg = OrderLeg.ENTRY,
) -> OrderIntent:
    return OrderIntent(
        signal_id="sig-12345678", leg=leg, trading_symbol=SYMBOL, side=side, quantity=qty,
        order_type=order_type, price=price, trigger_price=trigger, attempt=attempt,
    )


def test_a_market_buy_fills_at_the_ask_and_a_market_sell_at_the_bid() -> None:
    venue, _ = _rig()
    buy = venue.submit(_order("BUY"))
    assert buy.status is OrderStatus.FILLED and buy.avg_price == 100.0 and buy.filled_qty == 75
    assert buy.mode == "dry_run" and buy.broker_order_id.startswith("DRY-ORB-")
    sell = venue.submit(_order("SELL", attempt=2, leg=OrderLeg.EXIT))
    assert sell.status is OrderStatus.FILLED and sell.avg_price == 99.0
    assert venue.net_quantities() == {}


def test_the_ledger_tracks_net_quantity() -> None:
    venue, _ = _rig()
    venue.submit(_order("BUY", qty=150))
    venue.submit(_order("SELL", qty=75, attempt=2, leg=OrderLeg.EXIT))
    assert venue.net_quantities() == {SYMBOL: 75}


def test_selling_more_than_held_goes_negative_so_a_double_sell_is_visible() -> None:
    venue, _ = _rig()
    venue.submit(_order("BUY"))
    venue.submit(_order("SELL", attempt=2, leg=OrderLeg.EXIT))
    venue.submit(_order("SELL", attempt=3, leg=OrderLeg.EXIT))
    assert venue.net_quantities() == {SYMBOL: -75}


def test_a_limit_buy_rests_until_the_ask_comes_down_to_it() -> None:
    venue, quotes = _rig()
    order = venue.submit(_order("BUY", "LIMIT", price=98.0))
    assert order.status is OrderStatus.ACKNOWLEDGED
    quotes.set(97.0, 97.5)
    venue.poll(order)
    assert order.status is OrderStatus.FILLED and order.avg_price == 97.5


def test_a_limit_sell_needs_the_bid_at_or_above_its_price() -> None:
    venue, quotes = _rig()
    order = venue.submit(_order("SELL", "LIMIT", price=101.0, leg=OrderLeg.EXIT))
    assert order.status is OrderStatus.ACKNOWLEDGED
    quotes.set(101.5, 102.0)
    venue.poll(order)
    assert order.status is OrderStatus.FILLED and order.avg_price == 101.5


def test_cancel_takes_one_last_look_so_a_crossing_order_fills_first() -> None:
    venue, quotes = _rig()
    order = venue.submit(_order("BUY", "LIMIT", price=98.0))
    quotes.set(97.0, 97.5)                                 # it became fillable just now
    assert venue.cancel(order).status is OrderStatus.FILLED


def test_cancel_of_a_resting_order_and_of_a_finished_one() -> None:
    venue, _ = _rig()
    resting = venue.submit(_order("BUY", "LIMIT", price=90.0))
    assert venue.cancel(resting).status is OrderStatus.CANCELLED
    done = venue.submit(_order("BUY", attempt=2))
    assert venue.cancel(done).status is OrderStatus.FILLED   # nothing to cancel
    assert venue.poll(done).status is OrderStatus.FILLED


def test_no_quote_means_the_order_rests() -> None:
    venue, quotes = _rig()
    quotes.clear()
    order = venue.submit(_order("BUY"))
    assert order.status is OrderStatus.ACKNOWLEDGED
    quotes.set(99.0, 100.0)
    venue.poll(order)
    assert order.status is OrderStatus.FILLED


def test_a_one_sided_book_fills_nothing_on_the_missing_side() -> None:
    venue, quotes = _rig()
    quotes.set(None, 100.0)
    sell = venue.submit(_order("SELL", leg=OrderLeg.EXIT))
    assert sell.status is OrderStatus.ACKNOWLEDGED


def test_a_stop_loss_waits_for_its_trigger_then_acts_as_a_limit_order() -> None:
    venue, quotes = _rig()
    stop = venue.submit(
        _order("SELL", "SL", price=90.0, trigger=95.0, leg=OrderLeg.PROTECT)
    )
    assert stop.status is OrderStatus.ACKNOWLEDGED          # last 99 is above the trigger
    quotes.set(80.0, 81.0, ltp=94.0)                        # triggered, but a gap through the limit
    venue.poll(stop)
    assert stop.status is OrderStatus.ACKNOWLEDGED          # bid 80 < limit 90: no fill
    quotes.set(92.0, 93.0, ltp=92.5)                        # price comes back above the limit
    venue.poll(stop)
    assert stop.status is OrderStatus.FILLED and stop.avg_price == 92.0


def test_a_stop_loss_that_is_already_through_its_trigger_fills_at_the_bid() -> None:
    venue, quotes = _rig()
    quotes.set(94.0, 95.0, ltp=94.5)
    stop = venue.submit(_order("SELL", "SL", price=90.0, trigger=95.0, leg=OrderLeg.PROTECT))
    assert stop.status is OrderStatus.FILLED and stop.avg_price == 94.0


def test_a_buy_stop_triggers_upward() -> None:
    venue, quotes = _rig()
    stop = venue.submit(_order("BUY", "SL", price=110.0, trigger=105.0))
    assert stop.status is OrderStatus.ACKNOWLEDGED
    quotes.set(105.5, 106.0, ltp=106.0)
    venue.poll(stop)
    assert stop.status is OrderStatus.FILLED and stop.avg_price == 106.0


def test_bad_orders_are_rejected() -> None:
    venue, _ = _rig()
    assert venue.submit(_order(qty=0)).status is OrderStatus.REJECTED
    limit = venue.submit(_order("BUY", "LIMIT", price=None))
    assert limit.status is OrderStatus.REJECTED and "needs a price" in limit.reject_reason
    stop = venue.submit(_order("SELL", "SL", price=90.0, trigger=None))
    assert stop.status is OrderStatus.REJECTED and "trigger" in stop.reject_reason


def test_an_unknown_order_type_never_fills() -> None:
    venue, _ = _rig()
    assert venue.submit(_order(order_type="SL-M")).status is OrderStatus.ACKNOWLEDGED


def test_recent_fills_lists_the_fill_with_the_quote_it_met() -> None:
    venue, _ = _rig()
    fills = venue.recent_fills(venue.submit(_order("BUY")))
    assert len(fills) == 1 and fills[0].price == 100.0
    assert fills[0].raw["bid"] == 99.0 and fills[0].raw["ask"] == 100.0


def test_restore_ledger_only_fills_an_empty_ledger() -> None:
    venue, _ = _rig()
    venue.restore_ledger({SYMBOL: 75, "ZERO": 0})
    assert venue.net_quantities() == {SYMBOL: 75}
    venue.restore_ledger({SYMBOL: 999})                      # a running session is not rewritten
    assert venue.net_quantities() == {SYMBOL: 75}
