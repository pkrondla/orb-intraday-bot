"""Fakes for the executor tests: quotes, an account, a scripted broker and a ready-made rig."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from orb_intraday_bot.core.clock import IST, FixedClock
from orb_intraday_bot.execution.account import DryRunAccount
from orb_intraday_bot.execution.dry_run_venue import DryRunVenue
from orb_intraday_bot.execution.models import (
    FillReport,
    OrderIntent,
    OrderRecord,
    OrderStatus,
)
from orb_intraday_bot.execution.quotes import NoQuoteError, Quote
from orb_intraday_bot.risk.engine import RiskEngine
from orb_intraday_bot.risk.limits import RiskLimits
from orb_intraday_bot.runtime.executor import LiveExecutor
from orb_intraday_bot.storage.store import TradeStore
from tests.helpers import at

SYMBOL = "NIFTY26O19CE"          # the contract tests/unit/test_paper.make_trade builds


class Quotes:
    """A quote source the test moves by hand."""

    def __init__(self) -> None:
        self.table: dict[str, Quote] = {}
        self.now = at(9, 36)

    def set(
        self, bid: float | None, ask: float | None, ltp: float | None = None,
        symbol: str = SYMBOL,
    ) -> None:
        last = ltp if ltp is not None else (bid if bid is not None else ask)
        self.table[symbol] = Quote(symbol, bid, ask, last, self.now)

    def clear(self) -> None:
        self.table.clear()

    def quote(self, symbol: str) -> Quote:
        if symbol not in self.table:
            raise NoQuoteError(f"no quote for {symbol}")
        return self.table[symbol]


class AutoQuotes:
    """Quotes for any symbol: a fixed premium with a tight spread (for whole-session tests)."""

    def __init__(self, price: float = 150.0, half_spread: float = 0.1) -> None:
        self.price = price
        self.half_spread = half_spread
        self.now = at(9, 36)

    def quote(self, symbol: str) -> Quote:
        return Quote(
            symbol, round(self.price - self.half_spread, 2),
            round(self.price + self.half_spread, 2), self.price, self.now,
        )


class FakeAccount:
    def __init__(self) -> None:
        self.margin: float | None = 1_000_000.0
        self.positions: dict[str, int] | None = {}

    def available_option_margin(self) -> float | None:
        return self.margin

    def net_quantities(self) -> dict[str, int] | None:
        return self.positions


class ScriptedVenue:
    """A broker that does what the test says. A stop-loss order only fills when told to.

    `script[leg]` is a list of fill fractions, one per order of that leg, taken in order; once it
    runs out orders fill completely. 0 means the order rests, 0.5 fills half, and so on.
    """

    def __init__(self, clock: FixedClock) -> None:
        self._clock = clock
        self.orders: list[OrderRecord] = []
        self.script: dict[str, list[float]] = {}
        self.cancel_works = True
        self.fill_on_cancel = False
        self.fill_price = 100.0

    def submit(self, intent: OrderIntent) -> OrderRecord:
        now = self._clock.now()
        record = OrderRecord(intent=intent, created_at=now, updated_at=now, mode="live")
        record.transition(OrderStatus.SENT, at=now)
        record.broker_order_id = f"S-{len(self.orders) + 1}"
        record.transition(OrderStatus.ACKNOWLEDGED, at=now)
        self.orders.append(record)
        if intent.order_type != "SL":
            queue = self.script.get(intent.leg.value, [])
            fraction = queue.pop(0) if queue else 1.0
            quantity = int(intent.quantity * fraction)
            if quantity > 0:
                self.fill(record, quantity)
        return record

    def fill(self, record: OrderRecord, quantity: int, price: float | None = None) -> None:
        price = price if price is not None else (record.intent.price or self.fill_price)
        record.fills.append(
            FillReport(record.intent.idempotency_key, quantity, price, self._clock.now())
        )
        record.filled_qty, record.avg_price = quantity, price
        full = quantity >= record.intent.quantity
        record.transition(OrderStatus.FILLED if full else OrderStatus.PARTIAL)

    def cancel(self, record: OrderRecord) -> OrderRecord:
        if record.status in {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}:
            return record
        if self.fill_on_cancel:
            self.fill(record, record.intent.quantity, self.fill_price)
        elif self.cancel_works:
            record.transition(OrderStatus.CANCELLED)
        return record

    def poll(self, record: OrderRecord) -> OrderRecord:
        return record

    def recent_fills(self, record: OrderRecord) -> list[FillReport]:
        return list(record.fills)

    def of_leg(self, leg: str) -> list[OrderRecord]:
        return [o for o in self.orders if o.intent.leg.value == leg]


@dataclass
class Rig:
    executor: LiveExecutor
    venue: DryRunVenue | ScriptedVenue
    quotes: Quotes
    account: FakeAccount | DryRunAccount
    store: TradeStore
    risk: RiskEngine
    clock: FixedClock
    limits: RiskLimits
    sleeps: list[float] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)


def make_rig(
    tmp_path: Path,
    *,
    kind: str = "dry_run",
    limits: RiskLimits | None = None,
    on_sleep: Callable[[float], None] | None = None,
    start: datetime | None = None,
) -> Rig:
    """`kind` is "dry_run" (the simulated venue) or "live" (a scripted broker)."""
    limits = limits or RiskLimits()
    clock = FixedClock(start or at(9, 36).astimezone(IST))
    quotes = Quotes()
    quotes.set(149.8, 150.2)
    store = TradeStore(tmp_path / "orb.db")
    risk = RiskEngine(limits=limits)
    risk.reset_day(clock.now().date())
    sleeps: list[float] = []
    alerts: list[str] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds=seconds)
        if on_sleep is not None:
            on_sleep(seconds)

    venue: DryRunVenue | ScriptedVenue
    account: FakeAccount | DryRunAccount
    if kind == "dry_run":
        dry = DryRunVenue(quotes, clock)
        venue = dry
        funds = FakeAccount()
        account = DryRunAccount(dry, funds)
        mode = "dry_run"
    else:
        venue = ScriptedVenue(clock)
        account = FakeAccount()
        mode = "live"
    executor = LiveExecutor(
        mode=mode, venue=venue, quotes=quotes, account=account, limits=limits, clock=clock,
        sleep=sleep, risk=risk, store=store, notify=alerts.append,
    )
    return Rig(executor, venue, quotes, account, store, risk, clock, limits, sleeps, alerts)
