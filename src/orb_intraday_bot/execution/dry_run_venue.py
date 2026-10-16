"""Dry-run venue: the broker order step, skipped.

Everything the bot does around an order (risk gate, order state machine, polling, protective
stop, reconciliation, database, alerts) is the same as live. Only the call to the broker is
replaced by this venue, which fills orders against real bid/ask quotes the way an exchange would:

- a market buy fills at the ask, a market sell at the bid
- a limit buy fills only while the ask is at or below its price, at the ask; a limit sell only
  while the bid is at or above its price, at the bid
- a stop-loss sell triggers when the last price falls to its trigger, then behaves as a limit sell
  at its limit price, so a gap through the limit leaves it unfilled (as it would at an exchange)
- nothing stops a sell beyond the position held: the net quantity goes negative, which the
  reconciliation then reports. A double-sell bug shows up in a dry run instead of hiding.

No order id here ever reaches Groww. Ids look like `DRY-<reference>`.
"""

from __future__ import annotations

from orb_intraday_bot.core.clock import IST, Clock, SystemClock
from orb_intraday_bot.core.errors import DataUnavailableError
from orb_intraday_bot.execution.models import (
    FillReport,
    OrderIntent,
    OrderRecord,
    OrderStatus,
)
from orb_intraday_bot.execution.quotes import Quote, QuoteSource

_TERMINAL = {OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED}


class DryRunVenue:
    def __init__(self, quotes: QuoteSource, clock: Clock | None = None) -> None:
        self._quotes = quotes
        self._clock = clock or SystemClock()
        self._orders: dict[str, OrderRecord] = {}
        self._triggered: set[str] = set()
        self._net: dict[str, int] = {}

    # -- the venue port ------------------------------------------------------------------
    def submit(self, intent: OrderIntent) -> OrderRecord:
        now = self._clock.now().astimezone(IST)
        record = OrderRecord(intent=intent, created_at=now, updated_at=now, mode="dry_run")
        record.transition(OrderStatus.SENT, at=now)
        if intent.quantity <= 0:
            record.transition(OrderStatus.REJECTED, at=now)
            record.reject_reason = "quantity must be positive"
        elif intent.order_type in {"LIMIT", "SL"} and (intent.price or 0) <= 0:
            record.transition(OrderStatus.REJECTED, at=now)
            record.reject_reason = f"{intent.order_type} order needs a price"
        elif intent.order_type == "SL" and (intent.trigger_price or 0) <= 0:
            record.transition(OrderStatus.REJECTED, at=now)
            record.reject_reason = "SL order needs a trigger price"
        else:
            record.broker_order_id = f"DRY-{intent.reference_id}"
            record.transition(OrderStatus.ACKNOWLEDGED, at=now)
            self._orders[record.broker_order_id] = record
            self._try_fill(record)
        return record

    def cancel(self, record: OrderRecord) -> OrderRecord:
        if record.status in _TERMINAL:
            return record
        # One last look: an order that could fill right now has filled before the cancel lands.
        self._try_fill(record)
        if record.status not in _TERMINAL:
            record.transition(OrderStatus.CANCELLED, at=self._clock.now().astimezone(IST))
        return record

    def poll(self, record: OrderRecord) -> OrderRecord:
        if record.status not in _TERMINAL:
            self._try_fill(record)
        return record

    def recent_fills(self, record: OrderRecord) -> list[FillReport]:
        return list(record.fills)

    # -- the simulated account -----------------------------------------------------------
    def net_quantities(self) -> dict[str, int]:
        """Net quantity per symbol, like the broker's positions (zero entries are dropped)."""
        return {s: q for s, q in self._net.items() if q != 0}

    def restore_ledger(self, net: dict[str, int]) -> None:
        """After a restart: the ledger starts again from the positions the database holds.

        A process restart loses the venue's memory, but the positions it simulated are in the
        database. Only used while the ledger is empty, so a running session is never rewritten.
        """
        if not self._net:
            self._net = {s: q for s, q in net.items() if q != 0}

    # -- matching --------------------------------------------------------------------------
    def _try_fill(self, record: OrderRecord) -> None:
        intent = record.intent
        try:
            quote = self._quotes.quote(intent.trading_symbol)
        except DataUnavailableError:
            return                                   # no quote: the order simply rests
        price = self._match_price(record, quote)
        if price is None:
            return
        now = self._clock.now().astimezone(IST)
        record.fills.append(
            FillReport(
                intent_key=intent.idempotency_key,
                quantity=intent.quantity,
                price=price,
                time=now,
                broker_order_id=record.broker_order_id,
                raw={"dry_run": True, "bid": quote.bid, "ask": quote.ask, "ltp": quote.ltp},
            )
        )
        record.filled_qty = intent.quantity
        record.avg_price = price
        record.transition(OrderStatus.FILLED, at=now)
        sign = 1 if intent.side == "BUY" else -1
        self._net[intent.trading_symbol] = (
            self._net.get(intent.trading_symbol, 0) + sign * intent.quantity
        )

    def _match_price(self, record: OrderRecord, quote: Quote) -> float | None:
        intent = record.intent
        buy = intent.side == "BUY"
        bid, ask = _positive(quote.bid), _positive(quote.ask)
        if intent.order_type == "MARKET":
            return ask if buy else bid
        if intent.order_type == "LIMIT":
            limit = float(intent.price or 0)
            if buy:
                return ask if ask is not None and ask <= limit else None
            return bid if bid is not None and bid >= limit else None
        if intent.order_type == "SL":
            key = record.broker_order_id
            trigger = float(intent.trigger_price or 0)
            last = _positive(quote.ltp) or bid
            if key not in self._triggered:
                hit = last is not None and (last <= trigger if not buy else last >= trigger)
                if not hit:
                    return None
                self._triggered.add(key)
            limit = float(intent.price or 0)
            if buy:
                return ask if ask is not None and ask <= limit else None
            return bid if bid is not None and bid >= limit else None
        return None


def _positive(value: float | None) -> float | None:
    return value if value is not None and value > 0 else None
