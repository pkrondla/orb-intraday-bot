"""Core domain models. Kept small in Phase 1; extended as setups are implemented."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class Zone(StrEnum):
    BULLISH = "BULLISH"    # above R1
    BEARISH = "BEARISH"    # below S1
    NEUTRAL = "NEUTRAL"    # R1 .. S1


@dataclass(frozen=True, slots=True)
class Candle:
    """A closed 3-minute futures candle. `start` is the candle's opening time (IST)."""

    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    oi: float | None = None

    def __post_init__(self) -> None:
        if self.start.tzinfo is None:
            raise ValueError("Candle.start must be timezone-aware")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError(f"Inconsistent candle at {self.start}: {self}")
