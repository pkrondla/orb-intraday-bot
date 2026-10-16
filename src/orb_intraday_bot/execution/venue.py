"""Execution venue port. Paper, shadow and Groww all implement this."""

from __future__ import annotations

from typing import Protocol

from orb_intraday_bot.execution.models import FillReport, OrderIntent, OrderRecord


class ExecutionVenue(Protocol):
    def submit(self, intent: OrderIntent) -> OrderRecord:
        """Send an order. Returns the record in SENT/ACKNOWLEDGED/FILLED/REJECTED."""
        ...

    def cancel(self, record: OrderRecord) -> OrderRecord:
        """Cancel a live order. No-op if already terminal."""
        ...

    def poll(self, record: OrderRecord) -> OrderRecord:
        """Refresh status / fills from the venue."""
        ...

    def recent_fills(self, record: OrderRecord) -> list[FillReport]:
        """Fills observed since the last poll (may be empty)."""
        ...
