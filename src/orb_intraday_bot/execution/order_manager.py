"""Order manager: idempotent submit, state machine, venue polling.

The strategy never talks to a venue directly. Intents come in; fills and status changes come out.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from orb_intraday_bot.core.clock import IST, Clock, SystemClock
from orb_intraday_bot.execution.models import FillReport, OrderIntent, OrderRecord, OrderStatus
from orb_intraday_bot.execution.venue import ExecutionVenue

OnFill = Callable[[OrderRecord, FillReport], None]
OnReject = Callable[[OrderRecord], None]


class OrderManager:
    def __init__(
        self,
        venue: ExecutionVenue,
        clock: Clock | None = None,
        on_fill: OnFill | None = None,
        on_reject: OnReject | None = None,
    ) -> None:
        self._venue = venue
        self._clock = clock or SystemClock()
        self._on_fill = on_fill
        self._on_reject = on_reject
        self._orders: dict[str, OrderRecord] = {}

    @property
    def orders(self) -> dict[str, OrderRecord]:
        return dict(self._orders)

    def submit(self, intent: OrderIntent) -> OrderRecord:
        existing = self._orders.get(intent.idempotency_key)
        if existing is not None:
            return existing
        record = self._venue.submit(intent)
        self._orders[intent.idempotency_key] = record
        for fill in record.fills:
            if self._on_fill is not None:
                self._on_fill(record, fill)
        if record.status is OrderStatus.REJECTED and self._on_reject is not None:
            self._on_reject(record)
        return record

    def cancel(self, key: str) -> OrderRecord | None:
        record = self._orders.get(key)
        if record is None:
            return None
        updated = self._venue.cancel(record)
        self._orders[key] = updated
        return updated

    def poll_open(self) -> list[OrderRecord]:
        changed: list[OrderRecord] = []
        for key, record in list(self._orders.items()):
            if record.status in {
                OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED
            }:
                continue
            before = record.status, record.filled_qty
            updated = self._venue.poll(record)
            self._orders[key] = updated
            for fill in self._venue.recent_fills(updated):
                if fill not in record.fills and self._on_fill is not None:
                    self._on_fill(updated, fill)
            if (updated.status, updated.filled_qty) != before:
                changed.append(updated)
        return changed

    def open_orders(self) -> list[OrderRecord]:
        return [
            r for r in self._orders.values()
            if r.status not in {
                OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED
            }
        ]

    def now(self) -> datetime:
        return self._clock.now().astimezone(IST)
