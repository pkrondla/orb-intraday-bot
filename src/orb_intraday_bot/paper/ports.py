"""What the paper engine needs from persistence, as a port.

The engine depends on this protocol, not on SQLite. `storage.store.TradeStore` implements it, and
a test can pass any small fake. This is what keeps `paper` below `storage` in the layer rules.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

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
