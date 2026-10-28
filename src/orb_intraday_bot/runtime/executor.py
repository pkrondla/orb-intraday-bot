"""The live executor: carries out the position engine's entries and exits at a venue.

The same code runs for live trading and for the dry run. Only the venue differs: the Groww venue
sends the order to the broker, the dry-run venue fills it against real bid/ask quotes. Everything
else is shared, so a dry run on the real market exercises the whole pipeline:

- the pre-trade risk gate (limits, spread, price sanity, margin) before any order
- an entry limit buy a little above the ask; wait a few seconds; cancel and skip if not filled
- a crash-net stop-loss sell kept at the broker (not the futures SL), in step with the position
  (T1 books all but a one-lot runner; the net is only a safety stop and is never raised)
- bot-managed exits (the position engine decides *when*, this executor makes it happen)
- order tracking in the database and the structured log
- reconciliation of the bot's books against the broker's positions

Safety rules the code follows:
- never sell without knowing the protective stop is out of the way (no double sell)
- never chase an entry: one limit order, then cancel
- an unconfirmed cancel is never assumed to have worked
- a mismatch with the broker blocks new entries and alerts; it never sells anything by itself
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime

from orb_intraday_bot.core.clock import IST, Clock
from orb_intraday_bot.core.errors import DataUnavailableError, EntryNotFilledError, ExitFailedError
from orb_intraday_bot.engine.signal_engine import PlannedTrade
from orb_intraday_bot.execution.account import AccountReader
from orb_intraday_bot.execution.dry_run_venue import DryRunVenue
from orb_intraday_bot.execution.models import (
    OrderIntent,
    OrderLeg,
    OrderRecord,
    OrderStatus,
)
from orb_intraday_bot.execution.order_manager import OrderManager
from orb_intraday_bot.execution.quotes import (
    Quote,
    QuoteSource,
    round_down_to_tick,
    round_up_to_tick,
)
from orb_intraday_bot.execution.venue import ExecutionVenue
from orb_intraday_bot.options.pricing import OptionPlan
from orb_intraday_bot.paper.models import ExitReason, Position
from orb_intraday_bot.paper.ports import EntryExecution, ExitExecution, StopFill
from orb_intraday_bot.risk.engine import RiskEngine
from orb_intraday_bot.risk.limits import RiskLimits
from orb_intraday_bot.runtime.logs import log_event
from orb_intraday_bot.storage.store import TradeStore

log = logging.getLogger("orb.executor")

_TERMINAL = frozenset({OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED})
_ALERT_EVERY_SEC = 300.0
_MARKET_RETRIES = 3


def recorded_fill_price(record: OrderRecord, *, fallback: float = 0.0) -> float:
    """Groww sometimes reports a fill with quantity and `average_fill_price` 0.

    Prefer the broker average. If it is missing, the stop trigger, then the limit,
    then the caller's fallback (the position's entry premium). Never return 0 when
    any of those is known — booking a sale at ₹0 wipes the premium as a loss.
    """
    if record.avg_price > 0:
        return float(record.avg_price)
    intent = record.intent
    for label, value in (
        ("trigger", intent.trigger_price),
        ("limit", intent.price),
        ("entry", fallback),
    ):
        if value is not None and float(value) > 0:
            log.warning(
                "broker fill price missing for %s; using %s %.2f",
                intent.trading_symbol, label, float(value),
            )
            return float(value)
    return 0.0


class LiveExecutor:
    def __init__(
        self,
        *,
        mode: str,
        venue: ExecutionVenue,
        quotes: QuoteSource,
        account: AccountReader,
        limits: RiskLimits,
        clock: Clock,
        sleep: Callable[[float], None],
        risk: RiskEngine | None = None,
        store: TradeStore | None = None,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self.mode = mode
        self._venue = venue
        self._quotes = quotes
        self._account = account
        self._limits = limits
        self._clock = clock
        self._sleep = sleep
        self._risk = risk
        self._store = store
        self._notify = notify
        self._orders = OrderManager(venue, clock)
        self._attempts: dict[tuple[str, str], int] = {}
        self._stops: dict[str, OrderRecord] = {}
        self._last_alert: dict[str, datetime] = {}
        self._last_reconcile: datetime | None = None

    # ==================================================================================
    # entry
    # ==================================================================================
    def enter(self, trade: PlannedTrade, plan: OptionPlan, now: datetime) -> EntryExecution:
        signal_id = trade.signal.signal_id
        contract = plan.contract
        symbol, tick = contract.trading_symbol, contract.tick_size
        lots = min(plan.lots, self._limits.max_lots_per_trade)
        quantity = lots * contract.lot_size
        protection = self._limits.entry_price_protection_pct

        quote = self._get_quote(symbol)
        if quote is None or quote.ask is None or quote.ask <= 0 or quote.bid is None:
            self._veto(signal_id, symbol, "no two-sided option quote")
        assert quote is not None and quote.ask is not None          # for the type checker
        price_sane = quote.ask <= plan.entry_ltp * (1 + protection)
        limit_price = round_up_to_tick(quote.ask * (1 + protection), tick)
        margin = self._safe_margin()
        decision = (
            self._risk.check_entry(
                lots=lots,
                option_spread_pct=quote.spread_pct,
                available_margin_inr=margin,
                required_margin_inr=limit_price * quantity,
                price_sane=price_sane,
            )
            if self._risk is not None
            else None
        )
        if decision is not None and not decision.allowed:
            self._veto(signal_id, symbol, decision.reason)

        intent = OrderIntent(
            signal_id=signal_id,
            leg=OrderLeg.ENTRY,
            trading_symbol=symbol,
            side="BUY",
            quantity=quantity,
            order_type="LIMIT",
            price=limit_price,
            product=self._limits.product,
            attempt=self._next_attempt(signal_id, OrderLeg.ENTRY),
        )
        record = self._send(intent, "entry order sent", ask=quote.ask, bid=quote.bid)
        if record.status is OrderStatus.REJECTED:
            self._veto(signal_id, symbol, f"entry rejected by the broker: {record.reject_reason}")
        record = self._wait(record, self._limits.entry_fill_timeout_sec)
        if record.status is not OrderStatus.FILLED:
            record = self._cancel(record)
        if record.status is not OrderStatus.FILLED:
            if record.status not in _TERMINAL:
                self._alert(
                    f"cancel-{signal_id}",
                    f"CRITICAL: the entry order for {symbol} could not be cancelled and its "
                    f"state is unknown ({record.status.value}). Check the broker now. "
                    "Entries are blocked.",
                )
                if self._risk is not None:
                    self._risk.block_entries(f"entry order for {symbol} in an unknown state")
                raise EntryNotFilledError(f"entry for {symbol} could not be cancelled")
            if record.filled_qty > 0:
                self._flatten_partial_entry(record, signal_id, symbol, tick)
                self._veto(
                    signal_id, symbol,
                    f"entry only partly filled ({record.filled_qty}/{quantity}): sold back",
                )
            self._veto(signal_id, symbol, "entry not filled in time: cancelled, not chasing")

        price = record.avg_price if record.avg_price > 0 else limit_price
        log_event(
            log, "entry_filled", f"entry filled {symbol}", mode=self.mode, signal_id=signal_id,
            symbol=symbol, quantity=record.filled_qty, price=price, planned=plan.entry_ltp,
            ask=quote.ask, broker_order_id=record.broker_order_id,
        )
        return EntryExecution(price=price, lots=lots, order_id=record.broker_order_id)

    def _veto(self, signal_id: str, symbol: str, reason: str) -> None:
        log_event(
            log, "entry_vetoed", f"entry vetoed {symbol}: {reason}", mode=self.mode,
            signal_id=signal_id, symbol=symbol, reason=reason,
        )
        raise EntryNotFilledError(reason)

    def _flatten_partial_entry(
        self, record: OrderRecord, signal_id: str, symbol: str, tick: float
    ) -> None:
        """Sell back whatever part of the entry filled. Nothing here is allowed to stay open."""
        sold = self._sell_remaining(
            signal_id=signal_id, leg=OrderLeg.FLATTEN, symbol=symbol,
            quantity=record.filled_qty, tick=tick,
        )
        if sold[0] < record.filled_qty:
            left = record.filled_qty - sold[0]
            self._alert(
                f"partial-{signal_id}",
                f"CRITICAL: {left} of {symbol} bought on a partial entry could not be sold. "
                "Close it by hand. Entries are blocked.",
            )
            if self._risk is not None:
                self._risk.block_entries(f"partial entry on {symbol} not flattened")

    # ==================================================================================
    # the protective stop at the broker
    # ==================================================================================
    def protect(self, position: Position, now: datetime) -> None:
        """Keep the broker crash-net stop at the option safety-net (not the futures SL).

        All strategy exits (SL, T1, T2, close-through) fire on futures. This Groww order is
        only a dead-bot safety net and is never raised after a worse fill.
        """
        if position.order_mode != self.mode or not position.is_open:
            return
        quantity = position.remaining_quantity - position.unbooked_qty
        tick = position.tick_size
        trigger = round_up_to_tick(position.active_option_stop, tick)
        limit = round_down_to_tick(trigger * (1 - self._limits.stop_limit_buffer_pct), tick)
        if limit >= trigger:
            limit = round_down_to_tick(trigger - tick, tick)
        if quantity <= 0 or limit < tick:
            return
        if (
            position.stop_order_id
            and abs(position.stop_trigger - trigger) < tick / 2
            and position.stop_quantity == quantity
        ):
            return
        if position.stop_order_id and not self._cancel_stop(position):
            return                      # the old stop could not be taken away: keep it, retry
        intent = OrderIntent(
            signal_id=position.position_id,
            leg=OrderLeg.PROTECT,
            trading_symbol=position.trading_symbol,
            side="SELL",
            quantity=quantity,
            order_type="SL",
            price=limit,
            trigger_price=trigger,
            product=self._limits.product,
            attempt=self._next_attempt(position.position_id, OrderLeg.PROTECT),
        )
        record = self._send(intent, "protective stop sent", stop_limit=limit)
        if record.status is OrderStatus.REJECTED:
            self._alert(
                f"stop-{position.position_id}",
                f"WARNING: the protective stop for {position.trading_symbol} was rejected "
                f"({record.reject_reason}). The position has no broker-side stop; the bot "
                "still manages the exit.",
            )
            return
        position.stop_order_id = record.broker_order_id
        position.stop_order_key = intent.idempotency_key
        position.stop_trigger = trigger
        position.stop_quantity = quantity
        self._stops[position.position_id] = record

    def _stop_record(self, position: Position) -> OrderRecord | None:
        record = self._stops.get(position.position_id)
        if record is None and self._store is not None and position.stop_order_key:
            record = self._store.load_order(position.stop_order_key)
            if record is not None:
                self._stops[position.position_id] = record
        return record

    def _clear_stop_fields(self, position: Position) -> None:
        position.stop_order_id = ""
        position.stop_order_key = ""
        position.stop_trigger = 0.0
        position.stop_quantity = 0
        self._stops.pop(position.position_id, None)

    def _cancel_stop(self, position: Position) -> bool:
        """Take the protective stop off. False if it did not go away (executed or unconfirmed)."""
        record = self._stop_record(position)
        if record is None:
            return False
        self._venue.poll(record)
        if record.status in {OrderStatus.CANCELLED, OrderStatus.REJECTED}:
            self._persist(record, "stop_gone")
            self._clear_stop_fields(position)
            return True
        if record.status is not OrderStatus.FILLED:
            record = self._cancel(record)
        if record.status is OrderStatus.CANCELLED:
            if record.filled_qty > 0:        # the stop executed in part before it was cancelled
                self._carry(
                    position, record.filled_qty,
                    recorded_fill_price(record, fallback=position.entry_ltp),
                )
            self._clear_stop_fields(position)
            return True
        return False

    def poll_stops(self, positions: Sequence[Position], now: datetime) -> list[StopFill]:
        fills: list[StopFill] = []
        for position in positions:
            if position.order_mode != self.mode or not position.stop_order_id:
                continue
            record = self._stop_record(position)
            if record is None:
                continue
            self._venue.poll(record)
            if record.status is OrderStatus.FILLED:
                price = recorded_fill_price(record, fallback=position.entry_ltp)
                if record.avg_price <= 0 and price > 0:
                    record.avg_price = price
                self._persist(record, "stop_filled")
                fills.append(StopFill(position.position_id, price, record.filled_qty, now))
            elif record.status in {OrderStatus.CANCELLED, OrderStatus.REJECTED}:
                self._persist(record, "stop_gone")
                self._clear_stop_fields(position)
                self._alert(
                    f"stop-gone-{position.position_id}",
                    f"WARNING: the protective stop for {position.trading_symbol} is no longer "
                    f"active at the broker ({record.status.value}). It will be placed again.",
                )
        return fills

    # ==================================================================================
    # exits
    # ==================================================================================
    def sell(self, position: Position, lots: int, reason: str, now: datetime) -> ExitExecution:
        if position.order_mode != self.mode:
            raise ExitFailedError("the position belongs to another order mode")
        requested = lots * position.lot_size
        if position.stop_order_id:
            gone = self._stop_executed(position)
            if gone is not None:
                price, quantity = gone
                return ExitExecution(
                    price, quantity, self.mode, reason=ExitReason.BROKER_STOP.value
                )
        need = requested - position.unbooked_qty
        sold_qty, value = self._sell_remaining(
            signal_id=position.position_id, leg=OrderLeg.EXIT, symbol=position.trading_symbol,
            quantity=need, tick=position.tick_size,
        )
        if sold_qty < need:
            position.unbooked_qty += sold_qty
            position.unbooked_value += value
            self.protect(position, now)         # put the safety stop back on what is left
            self._alert(
                f"exit-{position.position_id}",
                f"WARNING: exit ({reason}) for {position.trading_symbol} incomplete: sold "
                f"{position.unbooked_qty} of {requested}. The bot will retry.",
            )
            raise ExitFailedError(
                f"sold {sold_qty} of {need} {position.trading_symbol}; will retry"
            )
        total_qty = position.unbooked_qty + need
        total_value = position.unbooked_value + value
        position.unbooked_qty = 0
        position.unbooked_value = 0.0
        price = round(total_value / total_qty, 4) if total_qty > 0 else 0.0
        log_event(
            log, "exit_filled", f"exit filled {position.trading_symbol}", mode=self.mode,
            position_id=position.position_id, reason=reason, quantity=total_qty, price=price,
        )
        return ExitExecution(price, total_qty, self.mode)

    def _stop_executed(self, position: Position) -> tuple[float, int] | None:
        """Clear the stop before selling. Returns (price, qty) if the stop itself got executed.

        Raises `ExitFailedError` when the stop's state cannot be confirmed: selling then could
        sell the same units twice.
        """
        record = self._stop_record(position)
        if record is None:
            raise ExitFailedError("the protective stop is not on record: not selling blind")
        self._venue.poll(record)
        if record.status is OrderStatus.FILLED:
            price = recorded_fill_price(record, fallback=position.entry_ltp)
            if record.avg_price <= 0 and price > 0:
                record.avg_price = price
            self._persist(record, "stop_filled")
            return price, record.filled_qty
        if not self._cancel_stop(position):
            record = self._stops.get(position.position_id) or record
            if record.status is OrderStatus.FILLED:       # it executed during the cancel
                price = recorded_fill_price(record, fallback=position.entry_ltp)
                if record.avg_price <= 0 and price > 0:
                    record.avg_price = price
                self._persist(record, "stop_filled")
                return price, record.filled_qty
            raise ExitFailedError("the protective stop could not be cancelled: not selling")
        return None

    def _carry(self, position: Position, quantity: int, price: float) -> None:
        position.unbooked_qty += quantity
        position.unbooked_value += quantity * price

    def _sell_remaining(
        self, *, signal_id: str, leg: OrderLeg, symbol: str, quantity: int, tick: float
    ) -> tuple[int, float]:
        """Sell `quantity`: a limit just under the bid first, then market orders.

        Returns (units sold, rupees received). Stops sending as soon as an order's state is
        unknown (an unconfirmed cancel), so nothing can be sold twice.
        """
        if quantity <= 0:
            return 0, 0.0
        quote = self._get_quote(symbol)
        steps: list[tuple[str, float | None]] = []
        if quote is not None and quote.bid is not None and quote.bid > 0:
            guard = quote.bid * (1 - self._limits.exit_price_protection_pct)
            steps.append(("LIMIT", max(tick, round_down_to_tick(guard, tick))))
        steps += [("MARKET", None)] * _MARKET_RETRIES
        remaining, value = quantity, 0.0
        for order_type, price in steps:
            if remaining <= 0:
                break
            intent = OrderIntent(
                signal_id=signal_id, leg=leg, trading_symbol=symbol, side="SELL",
                quantity=remaining, order_type=order_type, price=price,
                product=self._limits.product, attempt=self._next_attempt(signal_id, leg),
            )
            record = self._send(intent, f"{leg.value.lower()} order sent")
            if record.status is OrderStatus.REJECTED:
                continue
            record = self._wait(record, self._limits.exit_fill_timeout_sec)
            if record.status not in _TERMINAL:
                record = self._cancel(record)
            if record.filled_qty > 0:
                unit = record.avg_price if record.avg_price > 0 else (price or 0.0)
                value += record.filled_qty * unit
                remaining -= record.filled_qty
            if record.status not in _TERMINAL:
                break
        return quantity - remaining, value

    # ==================================================================================
    # reconciliation
    # ==================================================================================
    def startup(self, positions: Sequence[Position], now: datetime) -> list[str]:
        """After a (re)start: rebuild the dry-run ledger, re-arm stops, check the books."""
        mine = self._mine(positions)
        if isinstance(self._venue, DryRunVenue):
            self._venue.restore_ledger(self._expected(mine))
        for position in mine:
            self.protect(position, now)
        return self.reconcile(positions, now)

    def maybe_reconcile(self, positions: Sequence[Position], now: datetime) -> list[str]:
        last = self._last_reconcile
        if last is not None and (now - last).total_seconds() < self._limits.reconcile_interval_sec:
            return []
        return self.reconcile(positions, now)

    def reconcile(self, positions: Sequence[Position], now: datetime) -> list[str]:
        """Compare the bot's open positions with the broker's. Returns the problems found."""
        self._last_reconcile = now
        mine = self._mine(positions)
        expected = self._expected(mine)
        problems: list[str] = []
        actual = self._account.net_quantities()
        if actual is not None:
            exact = isinstance(self._venue, DryRunVenue)      # the dry run's ledger is only ours
            for symbol, want in expected.items():
                have = actual.get(symbol, 0)
                if have < want or (exact and have != want):
                    problems.append(f"{symbol}: the bot holds {want}, the broker shows {have}")
            if exact:
                for symbol, have in actual.items():
                    if symbol not in expected and have != 0:
                        problems.append(f"{symbol}: the broker shows {have}, the bot holds 0")
        if self._store is not None:
            for signal_id in self._store.orphan_entries(now.astimezone(IST).date(), self.mode):
                problems.append(f"entry {signal_id} filled but no position is on record")
        for problem in problems:
            log_event(
                log, "reconcile_mismatch", problem, mode=self.mode,
                problem=problem,
            )
        if problems:
            self._alert(
                "reconcile",
                "CRITICAL: the bot's books and the broker disagree. New entries are blocked and "
                "nothing is sold automatically. Check the account.\n" + "\n".join(problems),
            )
            if self._risk is not None:
                self._risk.block_entries("reconciliation mismatch")
        return problems

    def available_margin(self) -> float | None:
        """Cash available to buy options (for the operator's /funds), or None if unknown."""
        return self._safe_margin()

    def held_quantities(self) -> dict[str, int] | None:
        """Net quantity per symbol as the broker (or the dry-run ledger) shows it."""
        try:
            return self._account.net_quantities()
        except Exception:  # noqa: BLE001 - a lookup for a report must never raise
            log.warning("could not read the broker positions", exc_info=True)
            return None

    def _mine(self, positions: Sequence[Position]) -> list[Position]:
        return [p for p in positions if p.is_open and p.order_mode == self.mode]

    @staticmethod
    def _expected(positions: Sequence[Position]) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in positions:
            held = p.remaining_quantity - p.unbooked_qty
            if held > 0:
                out[p.trading_symbol] = out.get(p.trading_symbol, 0) + held
        return out

    # ==================================================================================
    # plumbing
    # ==================================================================================
    def _get_quote(self, symbol: str) -> Quote | None:
        try:
            return self._quotes.quote(symbol)
        except DataUnavailableError:
            return None

    def _safe_margin(self) -> float | None:
        try:
            return self._account.available_option_margin()
        except Exception:  # noqa: BLE001 - a funds lookup must never block or crash the entry
            log.warning("could not read the available margin", exc_info=True)
            return None

    def _next_attempt(self, signal_id: str, leg: OrderLeg) -> int:
        key = (signal_id, leg.value)
        used = self._attempts.get(key, 0)
        if self._store is not None:
            try:
                used = max(used, self._store.attempts_used(signal_id, leg.value))
            except Exception:  # noqa: BLE001 - the local counter still keeps keys unique
                log.warning("could not read earlier attempts", exc_info=True)
        self._attempts[key] = used + 1
        return used + 1

    def _send(self, intent: OrderIntent, message: str, **fields: object) -> OrderRecord:
        record = self._orders.submit(intent)
        record.mode = self.mode
        log_event(
            log, "order_sent", message, mode=self.mode, key=intent.idempotency_key,
            symbol=intent.trading_symbol, side=intent.side, order_type=intent.order_type,
            quantity=intent.quantity, price=intent.price, trigger=intent.trigger_price,
            status=record.status.value, broker_order_id=record.broker_order_id, **fields,
        )
        self._persist(record, "order_sent")
        return record

    def _wait(self, record: OrderRecord, timeout: float) -> OrderRecord:
        waited, step = 0.0, self._limits.order_poll_sec
        self._venue.poll(record)
        while record.status not in _TERMINAL and waited < timeout:
            self._sleep(step)
            waited += step
            self._venue.poll(record)
        self._persist(record, "order_update")
        return record

    def _cancel(self, record: OrderRecord) -> OrderRecord:
        cancelled = self._orders.cancel(record.intent.idempotency_key) or self._venue.cancel(record)
        self._persist(cancelled, "order_cancel")
        return cancelled

    def _persist(self, record: OrderRecord, event: str) -> None:
        if self._store is None:
            return
        try:
            now = self._clock.now().astimezone(IST)
            self._store.save_order(record)
            self._store.append_event(
                event,
                {
                    "key": record.intent.idempotency_key,
                    "status": record.status.value,
                    "filled_qty": record.filled_qty,
                    "avg_price": record.avg_price,
                    "broker_order_id": record.broker_order_id,
                },
                signal_id=record.intent.signal_id,
                at=now,
                mode=self.mode,
            )
        except Exception:  # noqa: BLE001 - the order is already placed: keep trading, log it
            log.error("could not persist order %s", record.intent.idempotency_key, exc_info=True)

    def _alert(self, key: str, text: str) -> None:
        now = self._clock.now()
        last = self._last_alert.get(key)
        if last is not None and (now - last).total_seconds() < _ALERT_EVERY_SEC:
            return
        self._last_alert[key] = now
        log.warning(text)
        if self._notify is not None:
            try:
                self._notify(text)
            except Exception:  # noqa: BLE001 - an alert failure must not break trading
                log.warning("alert delivery failed", exc_info=True)
