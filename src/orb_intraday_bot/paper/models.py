"""Paper positions and fills.

A `Position` is one paper option trade (one signal). Every exit is a `Fill`; the position keeps
the whole history, so a trade can be rebuilt from the database after a restart.

P&L is reported two ways (spec section 8):
- futures points: signed futures move per unit, averaged over the lots, no charges
- option rupees: (exit premium - entry premium) x quantity, less all charges
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from orb_intraday_bot.core.models import Direction


class ExitReason(StrEnum):
    HARD_STOP = "HARD_STOP"            # futures price hit the stop
    BREAKEVEN_STOP = "BREAKEVEN_STOP"  # stop had moved to entry after T1
    CLOSE_STOP = "CLOSE_STOP"          # candle closed back beyond the level
    OPTION_STOP = "OPTION_STOP"        # option price hit its stop (live prices only)
    T1 = "T1"                          # notes Target (all lots, or all but one runner)
    T2 = "T2"                          # one-lot runner (T1 ± OR-range or nearer pivot)
    EOD = "EOD"                        # end of day
    SQUARE_OFF = "SQUARE_OFF"          # the intraday square-off time (before the broker's own)
    FLATTEN = "FLATTEN"                # an operator or kill-switch flatten
    MANUAL = "MANUAL"                  # the operator closed this one position (/exit)
    BROKER_STOP = "BROKER_STOP"        # the protective stop-loss at the broker was executed


class PositionStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class Fill:
    """One exit order."""

    time: datetime
    reason: ExitReason
    lots: int
    quantity: int
    futures_price: float
    option_price: float
    price_source: str            # "live" option LTP, or "modeled" from futures and delta
    option_gross: float          # (option_price - entry premium) x quantity
    futures_points: float        # signed move per unit in the trade's favour
    charges: float               # charges on this exit order

    @property
    def option_net(self) -> float:
        return round(self.option_gross - self.charges, 2)


@dataclass(slots=True)
class Position:
    position_id: str             # the signal id: one trade per signal, idempotent
    setup: str
    direction: Direction
    level_name: str
    sign_id: str
    zone: str
    opened_at: datetime
    # option
    trading_symbol: str
    kind: str                    # "CE" or "PE"
    strike: int
    expiry: str                  # ISO date
    lot_size: int
    tick_size: float
    delta_used: float
    entry_ltp: float
    entry_charges: float
    # futures
    futures_entry: float
    hard_stop: float             # futures price stop (the 35-point stop, or the touch level)
    close_exit_level: float | None
    t1: float                          # notes Target (futures)
    t2: float                          # runner: T1 ± OR-range or nearer pivot; equals t1 if no room
    # option-price equivalents
    sl_exit: float               # 35 × delta (sizing / R:R). Not the Groww trigger.
    t1_option: float
    t2_option: float
    # sizing
    lots: int
    t1_book_lots: int
    # state
    remaining_lots: int
    crash_net: float = 0.0       # Groww / OPTION_STOP trigger. 0 = use sl_exit (legacy).
    t2_trigger: float | None = None  # T2 fires here (proximity band); None = exact t2
    t1_trigger: float | None = None  # T1 fires here (proximity band); None = exact t1
    t1_done: bool = False
    breakeven_after_t1: bool = True   # leftover runner: futures stop moves to entry after T1
    strategy_hash: str = ""           # the rules this trade was opened under (config.fingerprint)
    stop_at_entry: bool = False
    status: PositionStatus = PositionStatus.OPEN
    closed_at: datetime | None = None
    # Start of the last closed candle applied to this position. A restart that replays the day's
    # candles skips the ones already applied, so exits are never counted twice.
    last_candle: datetime | None = None
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    # How the position was handled: "paper" (simulated), "dry_run" (the live pipeline with the
    # broker order step skipped) or "live" (real orders). The same fields are kept in all modes.
    order_mode: str = "paper"
    planned_entry: float = 0.0        # the option price the plan was made at (fill vs plan)
    entry_order_id: str = ""          # broker (or dry-run) order id of the entry
    stop_order_id: str = ""           # id of the protective stop-loss order at the broker
    stop_order_key: str = ""          # the bot's own idempotency key for that order
    stop_trigger: float = 0.0         # trigger price the protective stop was placed with
    stop_quantity: int = 0            # quantity the protective stop covers
    # Part of an exit that was sold at the broker but not yet booked (the sale did not complete).
    # The retry sells only what is left, so nothing is ever sold twice.
    unbooked_qty: int = 0
    unbooked_value: float = 0.0       # rupees received for those units (price x quantity)

    # -- derived -------------------------------------------------------------------------
    @property
    def sign(self) -> float:
        return 1.0 if self.direction is Direction.LONG else -1.0

    @property
    def quantity(self) -> int:
        return self.lots * self.lot_size

    @property
    def remaining_quantity(self) -> int:
        return self.remaining_lots * self.lot_size

    @property
    def t1_hit_level(self) -> float:
        """Futures price that counts as T1 (the proximity band, or the chart level)."""
        if self.t1_trigger is not None:
            return self.t1_trigger
        # 0.17 stored the T1 band on t2_trigger when T1 = T2.
        if abs(self.t1 - self.t2) <= self.tick_size and self.t2_trigger is not None:
            return self.t2_trigger
        return self.t1

    @property
    def t2_hit_level(self) -> float:
        """Futures price that counts as T2 (the proximity band, or the chart level)."""
        return self.t2 if self.t2_trigger is None else self.t2_trigger

    @property
    def is_open(self) -> bool:
        return self.status is PositionStatus.OPEN

    @property
    def active_stop(self) -> float:
        """The futures price stop in force (legacy: moves to entry after a partial T1)."""
        return self.futures_entry if self.stop_at_entry else self.hard_stop

    @property
    def active_option_stop(self) -> float:
        """Broker CE/PE crash net. T1 moves only the futures stop; this trigger never rises."""
        return self.crash_net if self.crash_net > 0 else self.sl_exit

    @property
    def gross_option_pnl(self) -> float:
        return round(sum(f.option_gross for f in self.fills), 2)

    @property
    def total_charges(self) -> float:
        return round(self.entry_charges + sum(f.charges for f in self.fills), 2)

    @property
    def net_option_pnl(self) -> float:
        """Option rupees after all charges (entry and exits)."""
        return round(self.gross_option_pnl - self.total_charges, 2)

    @property
    def futures_points(self) -> float:
        """Futures points per lot-weighted unit over the lots closed so far."""
        closed = sum(f.lots for f in self.fills)
        if closed == 0:
            return 0.0
        return round(sum(f.futures_points * f.lots for f in self.fills) / closed, 2)

    def unrealized(self, option_price: float) -> float:
        """Open P&L in option rupees on the lots still held (before exit charges)."""
        return round((option_price - self.entry_ltp) * self.remaining_quantity, 2)

    # -- persistence ---------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if name == "fills":
                out[name] = [_fill_to_dict(f) for f in value]
            elif isinstance(value, datetime):
                out[name] = value.isoformat()
            elif isinstance(value, StrEnum):
                out[name] = value.value
            elif isinstance(value, list):
                out[name] = list(value)
            else:
                out[name] = value
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Position:
        data = dict(raw)
        data["direction"] = Direction(data["direction"])
        data["status"] = PositionStatus(data["status"])
        data["opened_at"] = datetime.fromisoformat(data["opened_at"])
        if data.get("closed_at"):
            data["closed_at"] = datetime.fromisoformat(data["closed_at"])
        if data.get("last_candle"):
            data["last_candle"] = datetime.fromisoformat(data["last_candle"])
        data["fills"] = [_fill_from_dict(f) for f in data.get("fills", [])]
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


def _fill_to_dict(fill: Fill) -> dict[str, Any]:
    return {
        "time": fill.time.isoformat(),
        "reason": fill.reason.value,
        "lots": fill.lots,
        "quantity": fill.quantity,
        "futures_price": fill.futures_price,
        "option_price": fill.option_price,
        "price_source": fill.price_source,
        "option_gross": fill.option_gross,
        "futures_points": fill.futures_points,
        "charges": fill.charges,
    }


def _fill_from_dict(raw: dict[str, Any]) -> Fill:
    return Fill(
        time=datetime.fromisoformat(raw["time"]),
        reason=ExitReason(raw["reason"]),
        lots=int(raw["lots"]),
        quantity=int(raw["quantity"]),
        futures_price=float(raw["futures_price"]),
        option_price=float(raw["option_price"]),
        price_source=str(raw["price_source"]),
        option_gross=float(raw["option_gross"]),
        futures_points=float(raw["futures_points"]),
        charges=float(raw["charges"]),
    )
