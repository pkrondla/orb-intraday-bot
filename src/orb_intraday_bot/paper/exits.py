"""Exit rules for an open paper position (spec section 7).

A position is closed by the first of:

1. the futures price stop (35-point hard stop or the touch level), or the option stop
2. T1 (partial booking, then the stop moves to entry) and T2
3. a candle *close* back beyond the stop level (close-based setups: ORB, R2/S2 breakout)
4. the end of the day

The mandatory pivot exits (R2/S2 touched, R4/S4) are already T2: the target planner caps T2 at
them, so reaching T2 closes whatever is left. T2 also fires when the extreme comes within
`t2_proximity_points` of that level (a near-miss at S2/R2).

Prices are checked on a `Bar`. A live poll is a bar with open = high = low = close = the price.
A closed candle is a bar with its own OHLC. Inside one candle the order of events is unknown, so
the checks are deliberately conservative:

- the stop is checked before the targets, so a candle that reaches both is a loss
- after T1 the break-even stop only applies from the next bar
- a gap through a stop fills at the open (worse); a gap through a target also fills at the open

Fill prices: with a live option LTP (polls) the exit fills at that LTP. Without it (replay, or a
touch inside a closed candle) the option price is modelled from the futures price and delta:
`entry_premium + sign x (futures - futures_entry) x delta`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from orb_intraday_bot.config.settings import PaperConfig
from orb_intraday_bot.core.errors import ExitFailedError
from orb_intraday_bot.paper.charges import order_charges
from orb_intraday_bot.paper.models import ExitReason, Fill, Position, PositionStatus
from orb_intraday_bot.paper.ports import PositionExecutor

log = logging.getLogger("orb.exits")


@dataclass(frozen=True, slots=True)
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    is_candle: bool = False        # True for a closed 3-minute candle, False for a live poll

    @classmethod
    def tick(cls, time: datetime, price: float) -> Bar:
        return cls(time, price, price, price, price, is_candle=False)


@dataclass(frozen=True, slots=True)
class ExitEvent:
    position_id: str
    time: datetime
    message: str
    fill: Fill | None = None       # None for a state change with no order (stop moved to cost)


def modeled_option_price(position: Position, futures_price: float) -> float:
    price = position.entry_ltp + position.sign * (futures_price - position.futures_entry) * (
        position.delta_used
    )
    return max(position.tick_size, _tick(price, position.tick_size))


def _tick(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 4)


def _exit_price(
    position: Position,
    futures_price: float,
    option_ltp: float | None,
    cfg: PaperConfig,
    use_live: bool,
) -> tuple[float, str]:
    if use_live and option_ltp is not None:
        price, source = option_ltp, "live"
    else:
        price, source = modeled_option_price(position, futures_price), "modeled"
    price = max(position.tick_size, _tick(price - cfg.slippage_option_points, position.tick_size))
    return price, source


def _book(
    position: Position,
    time: datetime,
    reason: ExitReason,
    lots: int,
    futures_price: float,
    option_ltp: float | None,
    cfg: PaperConfig,
    use_live: bool,
    executor: PositionExecutor | None = None,
) -> Fill:
    lots = min(lots, position.remaining_lots)
    quantity = lots * position.lot_size
    if executor is None:
        price, source = _exit_price(position, futures_price, option_ltp, cfg, use_live)
    else:
        # The executor really sells (or dry-runs the sale) and reports what it got. It raises
        # ExitFailedError before anything below runs, so a failed sale leaves the position as it
        # was and the exit is simply tried again on the next poll.
        done = executor.sell(position, lots, reason.value, time)
        price, source = done.price, done.source
        if done.reason is not None:
            reason = ExitReason(done.reason)
        if done.quantity != quantity:
            quantity = done.quantity
            lots = min(max(quantity // position.lot_size, 1), position.remaining_lots)
    return _record(position, time, reason, lots, quantity, futures_price, price, source, cfg)


def _record(
    position: Position,
    time: datetime,
    reason: ExitReason,
    lots: int,
    quantity: int,
    futures_price: float,
    price: float,
    source: str,
    cfg: PaperConfig,
) -> Fill:
    """Write one exit fill into the position and close it when no lots are left."""
    if price <= 0 and position.entry_ltp > 0:
        log.warning(
            "exit fill price missing for %s; using entry %.2f so P&L is not booked at 0",
            position.trading_symbol, position.entry_ltp,
        )
        price = position.entry_ltp
    fill = Fill(
        time=time,
        reason=reason,
        lots=lots,
        quantity=quantity,
        futures_price=round(futures_price, 2),
        option_price=price,
        price_source=source,
        option_gross=round((price - position.entry_ltp) * quantity, 2),
        futures_points=round(position.sign * (futures_price - position.futures_entry), 2),
        charges=order_charges("SELL", price, quantity, cfg.charges).total,
    )
    position.fills.append(fill)
    position.remaining_lots -= lots
    if position.remaining_lots <= 0:
        position.status = PositionStatus.CLOSED
        position.closed_at = time
    return fill


def book_broker_stop(
    position: Position,
    time: datetime,
    price: float,
    quantity: int,
    futures_price: float,
    source: str,
    cfg: PaperConfig,
) -> ExitEvent:
    """Record that the protective stop-loss at the broker was executed (not ordered by the bot)."""
    lots = min(max(quantity // position.lot_size, 1), position.remaining_lots)
    fill = _record(
        position, time, ExitReason.BROKER_STOP, lots, lots * position.lot_size, futures_price,
        price, source, cfg,
    )
    return ExitEvent(position.position_id, time, _describe(position, fill), fill)


def _describe(position: Position, fill: Fill) -> str:
    return (
        f"{position.trading_symbol} {fill.reason.value}: sold {fill.lots} lot(s) at "
        f"{fill.option_price:.2f} ({fill.price_source}), futures {fill.futures_price:.1f} "
        f"({fill.futures_points:+.1f} pts), option P&L Rs {fill.option_gross:+,.0f} "
        f"before charges"
    )


def evaluate_bar(
    position: Position,
    bar: Bar,
    option_ltp: float | None,
    cfg: PaperConfig,
    executor: PositionExecutor | None = None,
) -> list[ExitEvent]:
    """Apply the exit rules to one bar. Mutates the position and returns what happened.

    With an executor, a sale that fails is logged and left for the next bar or poll (the position
    keeps its state), and what already happened on this bar is still returned.
    """
    # `opened_at` is the close of the signal candle; earlier bars belong to the signal itself.
    if not position.is_open or bar.time < position.opened_at:
        return []
    events: list[ExitEvent] = []
    try:
        _evaluate(position, bar, option_ltp, cfg, executor, events)
    except ExitFailedError as exc:
        log.warning("exit not completed for %s: %s", position.trading_symbol, exc)
    return events


def _evaluate(
    position: Position,
    bar: Bar,
    option_ltp: float | None,
    cfg: PaperConfig,
    executor: PositionExecutor | None,
    events: list[ExitEvent],
) -> None:
    d = position.sign
    live = not bar.is_candle       # a poll's option LTP is current; a candle's touch is not

    def close_all(reason: ExitReason, fut_price: float, use_live: bool) -> None:
        fill = _book(
            position, bar.time, reason, position.remaining_lots, fut_price, option_ltp, cfg,
            use_live, executor,
        )
        events.append(ExitEvent(position.position_id, bar.time, _describe(position, fill), fill))

    # 1. Stops: futures price, then the option price.
    stop = position.active_stop
    stop_touched = bar.low <= stop if d > 0 else bar.high >= stop
    if stop_touched:
        gap_fill = min(bar.open, stop) if d > 0 else max(bar.open, stop)
        reason = ExitReason.BREAKEVEN_STOP if position.stop_at_entry else ExitReason.HARD_STOP
        close_all(reason, gap_fill, live)
        return
    if option_ltp is not None and option_ltp <= position.active_option_stop:
        close_all(ExitReason.OPTION_STOP, bar.close, True)
        return

    # 2. Targets. T2 closes everything left; T1 books part and moves the stop to cost.
    if not position.t1_done and _reached(bar, position.t1, d):
        t1_fill_price = max(bar.open, position.t1) if d > 0 else min(bar.open, position.t1)
        if position.t1_book_lots > 0:
            # Booked before t1_done is set: if the sale fails, T1 is still pending next poll.
            fill = _book(
                position, bar.time, ExitReason.T1, position.t1_book_lots, t1_fill_price,
                option_ltp, cfg, live, executor,
            )
            events.append(
                ExitEvent(position.position_id, bar.time, _describe(position, fill), fill)
            )
        position.t1_done = True
        if position.breakeven_after_t1:
            position.stop_at_entry = True
            events.append(
                ExitEvent(
                    position.position_id,
                    bar.time,
                    f"{position.trading_symbol} T1 reached: stop moved to entry "
                    f"(futures {position.futures_entry:.1f}, option {position.entry_ltp:.2f})",
                )
            )
    if position.is_open and _reached(bar, position.t2_hit_level, d):
        t2_fill_price = _t2_fill_price(bar, position, d)
        close_all(ExitReason.T2, t2_fill_price, live)
        return

    # 3. A candle close back beyond the level (close-based stops only).
    if bar.is_candle and position.close_exit_level is not None and position.is_open:
        level = position.close_exit_level
        if (d > 0 and bar.close < level) or (d < 0 and bar.close > level):
            close_all(ExitReason.CLOSE_STOP, bar.close, True)


def _reached(bar: Bar, level: float, d: float) -> bool:
    return bar.high >= level if d > 0 else bar.low <= level


def _t2_fill_price(bar: Bar, position: Position, d: float) -> float:
    """Fill at T2 if tagged, else at the proximity band. A gap through T2 fills at the open."""
    level = position.t2 if _reached(bar, position.t2, d) else position.t2_hit_level
    return max(bar.open, level) if d > 0 else min(bar.open, level)


def close_at_end_of_day(
    position: Position,
    time: datetime,
    futures_price: float,
    option_ltp: float | None,
    cfg: PaperConfig,
    executor: PositionExecutor | None = None,
    reason: ExitReason = ExitReason.EOD,
) -> list[ExitEvent]:
    """Close what is left (end of day, square-off or a flatten, by `reason`).

    A sale that fails with an executor leaves the position open and is retried by the caller.
    """
    if not position.is_open:
        return []
    try:
        fill = _book(
            position, time, reason, position.remaining_lots, futures_price, option_ltp, cfg,
            True, executor,
        )
    except ExitFailedError as exc:
        log.warning("close not completed for %s: %s", position.trading_symbol, exc)
        return []
    return [ExitEvent(position.position_id, time, _describe(position, fill), fill)]
