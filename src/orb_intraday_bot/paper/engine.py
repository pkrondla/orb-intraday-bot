"""The paper engine: opens paper positions from planned trades and runs their exits.

Nothing here talks to a broker. It records what would have happened. All prices come in as
arguments (closed candles, live polls, option LTPs), and time comes from the data, so replay and
live use the same code.

Opening a position (at the close of the signal candle):
  entry premium = the option LTP at signal time (plus the configured slippage), the buy charges
  are booked immediately, and the signal id is the position id, so the same signal can never
  open twice (also across restarts, through the store).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, time, timedelta

from orb_intraday_bot.config.settings import StrategyConfig
from orb_intraday_bot.core.clock import IST
from orb_intraday_bot.core.models import Candle, Direction
from orb_intraday_bot.engine.signal_engine import PlannedTrade
from orb_intraday_bot.options.pricing import OptionPlan
from orb_intraday_bot.paper.charges import order_charges
from orb_intraday_bot.paper.exits import (
    Bar,
    ExitEvent,
    close_at_end_of_day,
    evaluate_bar,
    modeled_option_price,
)
from orb_intraday_bot.paper.models import Position
from orb_intraday_bot.storage.store import TradeStore


class PaperEngine:
    def __init__(self, config: StrategyConfig, store: TradeStore | None = None) -> None:
        self._cfg = config
        self._store = store
        self._open: dict[str, Position] = {}
        self._closed: list[Position] = []
        h, m = (int(x) for x in config.session.close.split(":"))
        self._close_time = time(h, m)
        if store is not None:
            for position in store.open_positions():
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

    # -- opening -------------------------------------------------------------------------
    def open(self, trade: PlannedTrade, plan: OptionPlan, opened_at: datetime) -> Position | None:
        """Open a paper position, or return None if this signal already has one."""
        signal = trade.signal
        position_id = signal.signal_id
        if position_id in self._open or any(p.position_id == position_id for p in self._closed):
            return None
        if self._store is not None and self._store.has_position(position_id):
            return None

        tick = plan.contract.tick_size
        slip = self._cfg.paper.slippage_option_points
        entry = round(round((plan.entry_ltp + slip) / tick) * tick, 4)
        quantity = plan.lots * plan.contract.lot_size
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
            sl_exit=plan.sl,
            t1_option=plan.t1,
            t2_option=plan.t2,
            lots=plan.lots,
            t1_book_lots=plan.t1_book_lots,
            remaining_lots=plan.lots,
            breakeven_after_t1=self._cfg.targets.move_sl_to_entry_after_t1,
            flags=list(plan.flags),
            notes=[
                f"entry: {signal.entry_basis}",
                f"stop: {trade.stop.basis}",
                f"T1: {trade.targets.t1_basis}",
                f"T2: {trade.targets.t2_basis}",
            ],
        )
        self._open[position_id] = position
        self._save(position, opened_at)
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
        events: list[ExitEvent] = []
        close_at = candle.start + timedelta(minutes=self._cfg.session.candle_minutes)
        end_of_day = close_at >= datetime.combine(
            candle.start.date(), self._close_time, tzinfo=IST
        )
        for position in list(self._open.values()):
            if position.last_candle is not None and candle.start <= position.last_candle:
                continue            # already applied (a restart replaying the day's candles)
            ltp = (option_ltps or {}).get(position.trading_symbol)
            events += evaluate_bar(position, bar, ltp, self._cfg.paper)
            if position.is_open:
                position.last_candle = candle.start
            if position.is_open and end_of_day:
                events += close_at_end_of_day(
                    position, close_at, candle.close, ltp, self._cfg.paper
                )
            self._after(position, close_at)
        return events

    def on_tick(
        self, time_: datetime, futures_price: float, option_ltps: Mapping[str, float] | None = None
    ) -> list[ExitEvent]:
        """Apply a live price poll (futures LTP and option LTPs) to every open position."""
        bar = Bar.tick(time_, futures_price)
        events: list[ExitEvent] = []
        for position in list(self._open.values()):
            ltp = (option_ltps or {}).get(position.trading_symbol)
            events += evaluate_bar(position, bar, ltp, self._cfg.paper)
            self._after(position, time_)
        return events

    def close_all(
        self,
        time_: datetime,
        futures_price: float,
        option_ltps: Mapping[str, float] | None = None,
    ) -> list[ExitEvent]:
        """End of day (or shutdown at the session end): close everything still open."""
        events: list[ExitEvent] = []
        for position in list(self._open.values()):
            ltp = (option_ltps or {}).get(position.trading_symbol)
            events += close_at_end_of_day(position, time_, futures_price, ltp, self._cfg.paper)
            self._after(position, time_)
        return events

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
    def _after(self, position: Position, now: datetime) -> None:
        if not position.is_open:
            self._open.pop(position.position_id, None)
            self._closed.append(position)
        self._save(position, now)

    def _save(self, position: Position, now: datetime) -> None:
        if self._store is not None:
            self._store.save_position(position, now)
