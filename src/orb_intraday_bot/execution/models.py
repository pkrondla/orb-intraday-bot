"""Order intents and the order state machine.

The strategy and position manager emit `OrderIntent` (with an idempotency key). The order manager
owns everything after that. Paper, shadow and live venues all speak this language.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class OrderLeg(StrEnum):
    ENTRY = "ENTRY"        # buy the option
    PROTECT = "PROTECT"    # broker-side stop-loss that stays at the exchange as a safety net
    EXIT = "EXIT"          # a bot-managed sell (stop, target, close-stop, square-off, flatten)
    STOP = "STOP"
    T1 = "T1"
    T2 = "T2"
    FLATTEN = "FLATTEN"
    EOD = "EOD"


class OrderStatus(StrEnum):
    NEW = "NEW"
    SENT = "SENT"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


# Legal transitions. Anything else is an invariant violation.
_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.NEW: frozenset({OrderStatus.SENT, OrderStatus.CANCELLED, OrderStatus.REJECTED}),
    OrderStatus.SENT: frozenset(
        {
            OrderStatus.ACKNOWLEDGED,
            OrderStatus.PARTIAL,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
        }
    ),
    OrderStatus.ACKNOWLEDGED: frozenset(
        {OrderStatus.PARTIAL, OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}
    ),
    OrderStatus.PARTIAL: frozenset(
        {OrderStatus.PARTIAL, OrderStatus.FILLED, OrderStatus.CANCELLED}
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
}


def can_transition(current: OrderStatus, new: OrderStatus) -> bool:
    return new in _TRANSITIONS.get(current, frozenset())


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """What the strategy wants. Idempotency key = signal_id + leg (+ attempt)."""

    signal_id: str
    leg: OrderLeg
    trading_symbol: str
    side: str                    # BUY or SELL
    quantity: int
    order_type: str              # MARKET, LIMIT, SL, SL-M
    price: float | None = None
    trigger_price: float | None = None
    product: str = "MIS"
    tag: str = ""
    attempt: int = 1

    @property
    def idempotency_key(self) -> str:
        return f"{self.signal_id}:{self.leg.value}:{self.attempt}"

    @property
    def reference_id(self) -> str:
        """Broker order_reference_id. Prefixed ORB- so we can reconcile on a shared account."""
        # Groww reference ids are short; keep under 20 chars.
        raw = f"ORB-{self.signal_id[-8:]}-{self.leg.value[:3]}{self.attempt}"
        return raw[:20]


@dataclass(slots=True)
class FillReport:
    intent_key: str
    quantity: int
    price: float
    time: datetime
    broker_order_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OrderRecord:
    intent: OrderIntent
    status: OrderStatus = OrderStatus.NEW
    broker_order_id: str = ""
    filled_qty: int = 0
    avg_price: float = 0.0
    reject_reason: str = ""
    mode: str = "paper"          # paper | dry_run | live: how the order was handled
    created_at: datetime | None = None
    updated_at: datetime | None = None
    fills: list[FillReport] = field(default_factory=list)

    def transition(self, new: OrderStatus, *, at: datetime | None = None) -> None:
        if new is self.status:
            return
        if not can_transition(self.status, new):
            raise ValueError(f"illegal order transition {self.status} -> {new}")
        self.status = new
        if at is not None:
            self.updated_at = at
