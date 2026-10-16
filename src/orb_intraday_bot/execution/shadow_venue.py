"""Shadow venue: real quotes, simulated orders.

Records what a real order would have filled at (bid/ask or LTP), without talking to the broker
order API. Used for the shadow trial before live arming.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from orb_intraday_bot.core.clock import Clock, SystemClock
from orb_intraday_bot.execution.models import FillReport, OrderIntent, OrderRecord, OrderStatus
from orb_intraday_bot.execution.paper_venue import PaperVenue

QuoteFn = Callable[[str], float | None]


@dataclass
class ShadowFill:
    intent_key: str
    symbol: str
    side: str
    quantity: int
    would_fill_price: float
    quote_time: str


@dataclass
class ShadowVenue:
    """Delegates fill simulation to PaperVenue, and keeps a would-have-filled log."""

    quote: QuoteFn
    clock: Clock = field(default_factory=SystemClock)
    log: list[ShadowFill] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._paper = PaperVenue(clock=self.clock, quote=self.quote)

    def submit(self, intent: OrderIntent) -> OrderRecord:
        record = self._paper.submit(intent)
        if record.status is OrderStatus.FILLED and record.fills:
            fill = record.fills[-1]
            self.log.append(
                ShadowFill(
                    intent_key=intent.idempotency_key,
                    symbol=intent.trading_symbol,
                    side=intent.side,
                    quantity=fill.quantity,
                    would_fill_price=fill.price,
                    quote_time=fill.time.isoformat(),
                )
            )
        return record

    def cancel(self, record: OrderRecord) -> OrderRecord:
        return self._paper.cancel(record)

    def poll(self, record: OrderRecord) -> OrderRecord:
        return self._paper.poll(record)

    def recent_fills(self, record: OrderRecord) -> list[FillReport]:
        return self._paper.recent_fills(record)
