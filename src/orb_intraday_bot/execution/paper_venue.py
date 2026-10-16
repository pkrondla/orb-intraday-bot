"""Paper venue: simulated fills. Used by replay and the paper serve path.

Fills are immediate at the intent price (or an injected quote). Slippage is applied by the
caller when building the intent, matching today's PaperEngine behaviour.
"""

from __future__ import annotations

from collections.abc import Callable

from orb_intraday_bot.core.clock import IST, Clock, SystemClock
from orb_intraday_bot.execution.models import (
    FillReport,
    OrderIntent,
    OrderRecord,
    OrderStatus,
)

QuoteFn = Callable[[str], float | None]


class PaperVenue:
    def __init__(
        self,
        clock: Clock | None = None,
        quote: QuoteFn | None = None,
        *,
        reject_keys: set[str] | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._quote = quote
        self._reject_keys = reject_keys or set()
        self._orders: dict[str, OrderRecord] = {}

    def submit(self, intent: OrderIntent) -> OrderRecord:
        now = self._clock.now().astimezone(IST)
        record = OrderRecord(intent=intent, created_at=now, updated_at=now)
        record.transition(OrderStatus.SENT, at=now)
        if intent.idempotency_key in self._reject_keys:
            record.transition(OrderStatus.REJECTED, at=now)
            record.reject_reason = "injected rejection"
            self._orders[intent.idempotency_key] = record
            return record
        price = intent.price
        if price is None and self._quote is not None:
            price = self._quote(intent.trading_symbol)
        if price is None:
            record.transition(OrderStatus.REJECTED, at=now)
            record.reject_reason = "no price"
            self._orders[intent.idempotency_key] = record
            return record
        record.transition(OrderStatus.ACKNOWLEDGED, at=now)
        fill = FillReport(
            intent_key=intent.idempotency_key,
            quantity=intent.quantity,
            price=float(price),
            time=now,
            broker_order_id=f"PAPER-{intent.reference_id}",
        )
        record.fills.append(fill)
        record.filled_qty = intent.quantity
        record.avg_price = float(price)
        record.broker_order_id = fill.broker_order_id
        record.transition(OrderStatus.FILLED, at=now)
        self._orders[intent.idempotency_key] = record
        return record

    def cancel(self, record: OrderRecord) -> OrderRecord:
        if record.status in {OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED}:
            return record
        now = self._clock.now().astimezone(IST)
        record.transition(OrderStatus.CANCELLED, at=now)
        return record

    def poll(self, record: OrderRecord) -> OrderRecord:
        return self._orders.get(record.intent.idempotency_key, record)

    def recent_fills(self, record: OrderRecord) -> list[FillReport]:
        return list(record.fills)
