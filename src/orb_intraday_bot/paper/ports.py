"""What the paper engine needs from the outside, as ports.

The engine depends on these protocols, not on SQLite or a broker. `storage.store.TradeStore`
implements `PositionRepository`; the runtime's `LiveExecutor` implements `PositionExecutor`.
That keeps `paper` below `storage` and `execution` in the layer rules.

With no executor the engine is the paper engine: it decides every exit and books it at a modelled
or polled price. With an executor the engine still decides *when* (the same rules), but the
executor makes the trade happen and reports the price it really got.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from orb_intraday_bot.engine.signal_engine import PlannedTrade
from orb_intraday_bot.options.pricing import OptionPlan
from orb_intraday_bot.paper.models import Position


class PositionRepository(Protocol):
    def has_position(self, position_id: str) -> bool:
        """True if a position with this id was ever saved (the idempotency check)."""
        ...

    def open_positions(self) -> list[Position]:
        """Positions still open, for recovery after a restart."""
        ...

    def save_position(self, position: Position, now: datetime) -> None:
        """Insert or update a position."""
        ...


@dataclass(frozen=True, slots=True)
class EntryExecution:
    """A filled entry: what was really bought."""

    price: float                 # average fill price of the option
    lots: int                    # lots filled (may be below the plan: the risk cap)
    order_id: str                # broker (or dry-run) id of the entry order


@dataclass(frozen=True, slots=True)
class ExitExecution:
    """A filled exit: what was really sold."""

    price: float                 # average fill price of the option
    quantity: int
    source: str                  # "live" or "dry_run"
    reason: str | None = None    # set when the exit was really the broker-side stop


@dataclass(frozen=True, slots=True)
class StopFill:
    """The protective stop-loss at the broker was executed (the bot did not order it)."""

    position_id: str
    price: float
    quantity: int
    time: datetime


class PositionExecutor(Protocol):
    """Makes entries and exits real (or a dry run of real) and keeps the protective stop."""

    mode: str                    # "dry_run" or "live"

    def enter(self, trade: PlannedTrade, plan: OptionPlan, now: datetime) -> EntryExecution:
        """Buy the option. Raises `EntryNotFilledError` if nothing was bought."""
        ...

    def protect(self, position: Position, now: datetime) -> None:
        """Place or refresh the broker-side stop-loss so it matches the position's state."""
        ...

    def sell(
        self, position: Position, lots: int, reason: str, now: datetime
    ) -> ExitExecution:
        """Sell `lots`. Raises `ExitFailedError` if it did not complete (position unchanged)."""
        ...

    def poll_stops(self, positions: Sequence[Position], now: datetime) -> list[StopFill]:
        """Protective stops that were executed at the broker since the last look."""
        ...
