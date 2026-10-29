"""The position engine: opens positions from planned trades and runs their exits.

Without an executor this is the paper engine: nothing here talks to a broker, it records what
would have happened. All prices come in as arguments (closed candles, live polls, option LTPs),
and time comes from the data, so replay and live use the same code.

With an executor (the dry run or live trading) the rules are exactly the same, but each entry and
each exit is carried out by the executor, and the position records the price it really got. The
executor also keeps a crash-net stop-loss at the broker (not the strategy SL); if that net is
executed, the engine books it here. Strategy SL, T1 and T2 are futures prices.

Opening a position (at the close of the signal candle):
  entry premium = the option LTP at signal time (plus the configured slippage), or, with an
  executor, the price the buy order filled at. The buy charges are booked immediately, and the
  signal id is the position id, so the same signal can never open twice (also across restarts,
  through the store).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from datetime import datetime, time, timedelta

from orb_intraday_bot.config.fingerprint import fingerprint
from orb_intraday_bot.config.settings import StrategyConfig
from orb_intraday_bot.core.clock import IST
from orb_intraday_bot.core.models import Candle, Direction
from orb_intraday_bot.engine.signal_engine import PlannedTrade
from orb_intraday_bot.options.pricing import OptionPlan, book_lots_at_t1, safety_net_option_stop
from orb_intraday_bot.paper.charges import order_charges
from orb_intraday_bot.paper.exits import (
    Bar,
    ExitEvent,
    book_broker_stop,
    close_at_end_of_day,
    evaluate_bar,
    modeled_option_price,
)
from orb_intraday_bot.paper.models import ExitReason, Position
from orb_intraday_bot.paper.ports import PositionExecutor, PositionRepository
from orb_intraday_bot.risk.targets import t2_trigger_price

PositionHook = Callable[[Position], None]


class PaperEngine:
    def __init__(
        self,
        config: StrategyConfig,
        store: PositionRepository | None = None,
        executor: PositionExecutor | None = None,
        executor_enabled: Callable[[], bool] | None = None,
        on_open: PositionHook | None = None,
        on_close: PositionHook | None = None,
    ) -> None:
        self._cfg = config
        self._strategy_hash = fingerprint(config).strategy
        self._store = store
        self._executor = executor
        self._executor_enabled = executor_enabled or (lambda: True)
        self._on_open = on_open
        self._on_close = on_close
        self._open: dict[str, Position] = {}
        self._closed: list[Position] = []
        h, m = (int(x) for x in config.session.close.split(":"))
        self._close_time = time(h, m)
        if store is not None:
            for position in store.open_positions():
                self._ensure_crash_net(position)
                self._open[position.position_id] = position

    # -- state ---------------------------------------------------------------------------
    @property
    def open_positions(self) -> list[Position]:
        return list(self._open.values())

    @property
    def closed_positions(self) -> list[Position]:
        return list(self._closed)

    def open_directions(self) -> list[Direction]:
        return [p.direction for p in self._open.values()]

    def has_position(self, position_id: str) -> bool:
        """True if this signal already has a position (open, closed today, or in the store)."""
        return (
            position_id in self._open
            or any(p.position_id == position_id for p in self._closed)
            or (self._store is not None and self._store.has_position(position_id))
        )

    def _entry_executor(self) -> PositionExecutor | None:
        """The executor for a new entry, if execution is on right now (armed, not paper)."""
        if self._executor is not None and self._executor_enabled():
            return self._executor
        return None

    def _executor_for(self, position: Position) -> PositionExecutor | None:
        """A position is always closed the way it was opened (never a dry-run exit on a live
        position, or the reverse)."""
        executor = self._executor
        if executor is not None and position.order_mode == executor.mode:
            return executor
        return None

    # -- opening -------------------------------------------------------------------------
    def open(self, trade: PlannedTrade, plan: OptionPlan, opened_at: datetime) -> Position | None:
        """Open a position, or return None if this signal already has one.

        With an executor this places the buy and raises `EntryNotFilledError` if nothing was
        bought (the caller releases the trade).
        """
        signal = trade.signal
        position_id = signal.signal_id
        if position_id in self._open or any(p.position_id == position_id for p in self._closed):
            return None
        if self._store is not None and self._store.has_position(position_id):
            return None

        tick = plan.contract.tick_size
        executor = self._entry_executor()
        if executor is None:
            slip = self._cfg.paper.slippage_option_points
            entry = round(round((plan.entry_ltp + slip) / tick) * tick, 4)
            lots, order_id, order_mode = plan.lots, "", "paper"
            sl_exit, t1_option, t2_option = plan.sl, plan.t1, plan.t2
            t1_book_lots = plan.t1_book_lots
        else:
            done = executor.enter(trade, plan, opened_at)
            entry = round(round(done.price / tick) * tick, 4)
            lots, order_id, order_mode = done.lots, done.order_id, executor.mode
            # R:R / sizing stay on the 35 × delta plan SL. A worse fill must not
            # pull that number or the Groww crash net closer to the market.
            sl_exit = plan.sl
            t1_option, t2_option = plan.t1, plan.t2
            single_target = abs(trade.targets.t1 - trade.targets.t2) < 1e-9
            t1_book_lots = (
                lots if single_target
                else (plan.t1_book_lots if lots == plan.lots else book_lots_at_t1(lots))
            )
        crash_net = self._crash_net(plan.sl, entry, plan.delta_used, tick)
        quantity = lots * plan.contract.lot_size
        position = Position(
            position_id=position_id,
            setup=signal.setup.value,
            direction=signal.direction,
            level_name=signal.level_name,
            sign_id=signal.sign_id,
            zone=trade.zone.value,
            opened_at=opened_at.astimezone(IST),
            trading_symbol=plan.contract.trading_symbol,
            kind=plan.contract.kind.value,
            strike=plan.contract.strike,
            expiry=plan.expiry.isoformat(),
            lot_size=plan.contract.lot_size,
            tick_size=tick,
            delta_used=plan.delta_used,
            entry_ltp=entry,
            entry_charges=order_charges("BUY", entry, quantity, self._cfg.paper.charges).total,
            futures_entry=signal.entry_price,
            hard_stop=trade.stop.price_stop,
            close_exit_level=trade.stop.close_exit_level,
            t1=trade.targets.t1,
            t2=trade.targets.t2,
            t1_trigger=t2_trigger_price(
                signal.entry_price,
                trade.targets.t1,
                long=signal.direction is Direction.LONG,
                proximity_points=self._cfg.targets.t2_proximity_points,
                min_stop_points=self._cfg.stops.min_stop_points,
            ),
            t2_trigger=t2_trigger_price(
                signal.entry_price,
                trade.targets.t2,
                long=signal.direction is Direction.LONG,
                proximity_points=self._cfg.targets.t2_proximity_points,
                min_stop_points=self._cfg.stops.min_stop_points,
            ),
            sl_exit=sl_exit,
            crash_net=crash_net,
            t1_option=t1_option,
            t2_option=t2_option,
            lots=lots,
            t1_book_lots=t1_book_lots,
            remaining_lots=lots,
            breakeven_after_t1=self._cfg.targets.move_sl_to_entry_after_t1,
            strategy_hash=self._strategy_hash,
            flags=list(plan.flags),
            notes=[
                f"entry: {signal.entry_basis}",
                f"stop: {trade.stop.basis}",
                f"T1: {trade.targets.t1_basis}",
                f"T2: {trade.targets.t2_basis}",
            ],
            order_mode=order_mode,
            planned_entry=plan.entry_ltp,
            entry_order_id=order_id,
        )
        self._open[position_id] = position
        self._save(position, opened_at)
        if executor is not None:
            # Saved first: if anything below fails, the filled entry is on record.
            executor.protect(position, opened_at)
            self._save(position, opened_at)
            if self._on_open is not None:
                self._on_open(position)
        return position

    # -- running exits -------------------------------------------------------------------
    def on_candle(
        self, candle: Candle, option_ltps: Mapping[str, float] | None = None
    ) -> list[ExitEvent]:
        """Apply a closed 3-minute candle to every open position (and close them at the end of
        the day). `option_ltps` (current premiums, by trading symbol) is optional: without it,
        fills are modelled from the futures price."""
        bar = Bar(
            candle.start, candle.open, candle.high, candle.low, candle.close, is_candle=True
        )
        close_at = candle.start + timedelta(minutes=self._cfg.session.candle_minutes)
        events = self._apply_broker_stops(close_at, candle.close)
        end_of_day = close_at >= datetime.combine(
            candle.start.date(), self._close_time, tzinfo=IST
        )
        for position in list(self._open.values()):
            if position.last_candle is not None and candle.start <= position.last_candle:
                continue            # already applied (a restart replaying the day's candles)
            executor = self._executor_for(position)
            ltp = (option_ltps or {}).get(position.trading_symbol)
            events += evaluate_bar(position, bar, ltp, self._cfg.paper, executor)
            if position.is_open:
                position.last_candle = candle.start
            if position.is_open and end_of_day:
                events += close_at_end_of_day(
                    position, close_at, candle.close, ltp, self._cfg.paper, executor
                )
            self._after(position, close_at)
        return events

    def on_tick(
        self, time_: datetime, futures_price: float, option_ltps: Mapping[str, float] | None = None
    ) -> list[ExitEvent]:
        """Apply a live price poll (futures LTP and option LTPs) to every open position."""
        bar = Bar.tick(time_, futures_price)
        events = self._apply_broker_stops(time_, futures_price)
        for position in list(self._open.values()):
            ltp = (option_ltps or {}).get(position.trading_symbol)
            events += evaluate_bar(
                position, bar, ltp, self._cfg.paper, self._executor_for(position)
            )
            self._after(position, time_)
        return events

    def close_all(
        self,
        time_: datetime,
        futures_price: float,
        option_ltps: Mapping[str, float] | None = None,
        reason: ExitReason = ExitReason.EOD,
        executed_only: bool = False,
        position_ids: Collection[str] | None = None,
    ) -> list[ExitEvent]:
        """Close everything still open: end of day, the square-off time, or a flatten.

        With an executor a position whose sale fails stays open (check `open_positions`) and the
        caller tries again. `executed_only` leaves simulated (paper) positions alone: the
        square-off time applies only to positions that hold real or dry-run orders.
        `position_ids` limits the close to those positions (the operator's /exit).
        """
        events: list[ExitEvent] = []
        for position in list(self._open.values()):
            if position_ids is not None and position.position_id not in position_ids:
                continue
            if executed_only and self._executor_for(position) is None:
                continue
            ltp = (option_ltps or {}).get(position.trading_symbol)
            events += close_at_end_of_day(
                position, time_, futures_price, ltp, self._cfg.paper,
                self._executor_for(position), reason,
            )
            self._after(position, time_)
        return events

    def protect_all(self, now: datetime) -> list[Position]:
        """Put the broker-side stop back on every executed position that has none (or a wrong
        one). Returns the positions it looked at; the stop fields say what was placed."""
        looked: list[Position] = []
        for position in list(self._open.values()):
            executor = self._executor_for(position)
            if executor is None:
                continue
            executor.protect(position, now)
            self._save(position, now)
            looked.append(position)
        return looked

    def mark_to_market(
        self, futures_price: float, option_ltps: Mapping[str, float] | None = None
    ) -> list[tuple[Position, float]]:
        """Each open position with its current premium (live LTP, else modelled)."""
        out: list[tuple[Position, float]] = []
        for position in self._open.values():
            ltp = (option_ltps or {}).get(position.trading_symbol)
            price = ltp if ltp is not None else modeled_option_price(position, futures_price)
            out.append((position, price))
        return out

    # -- internals -----------------------------------------------------------------------
    def _apply_broker_stops(self, now: datetime, futures_price: float) -> list[ExitEvent]:
        """Book any protective stop that the broker executed since the last look."""
        events: list[ExitEvent] = []
        executor = self._executor
        if executor is None:
            return events
        watched = [
            p for p in self._open.values() if p.order_mode == executor.mode and p.stop_order_id
        ]
        if not watched:
            return events
        for stop in executor.poll_stops(watched, now):
            position = self._open.get(stop.position_id)
            if position is None:
                continue
            events.append(
                book_broker_stop(
                    position, now, stop.price, stop.quantity, futures_price, executor.mode,
                    self._cfg.paper,
                )
            )
            position.stop_order_id = ""
            position.stop_quantity = 0
            self._after(position, now)
        return events

    def _after(self, position: Position, now: datetime) -> None:
        if not position.is_open:
            self._open.pop(position.position_id, None)
            self._closed.append(position)
            if self._on_close is not None and position.order_mode != "paper":
                self._on_close(position)
        else:
            executor = self._executor_for(position)
            if executor is not None:
                executor.protect(position, now)     # keeps the broker crash net on what's left
        self._save(position, now)

    def _crash_net(self, planned_sl: float, fill: float, delta_used: float, tick: float) -> float:
        return safety_net_option_stop(
            planned_sl=planned_sl,
            fill=fill,
            delta_used=delta_used,
            hard_stop_points=self._cfg.stops.hard_stop_points,
            tick=tick,
            min_delta=self._cfg.stops.crash_net_min_delta,
            extra_points=self._cfg.stops.crash_net_extra_points,
        )

    def _ensure_crash_net(self, position: Position) -> None:
        """Widen a legacy position whose Groww trigger was the 35 × delta option SL."""
        if position.crash_net > 0:
            return
        position.crash_net = self._crash_net(
            position.sl_exit, position.entry_ltp, position.delta_used, position.tick_size,
        )

    def _save(self, position: Position, now: datetime) -> None:
        if self._store is not None:
            self._store.save_position(position, now)
