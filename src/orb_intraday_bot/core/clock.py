"""Time helpers. All market logic runs in IST; a Clock is injected so replay and tests are exact."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
CANDLE_MINUTES = 3


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC).astimezone(IST)


class FixedClock:
    """A settable clock for tests and replay."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._now = start.astimezone(IST)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value.astimezone(IST)

    def advance(self, **kwargs: float) -> None:
        self._now = self._now + timedelta(**kwargs)


def candle_start(ts: datetime) -> datetime:
    """Start of the 3-minute candle containing `ts`, anchored at 09:15."""
    local = ts.astimezone(IST)
    anchor = local.replace(hour=9, minute=15, second=0, microsecond=0)
    elapsed = int((local - anchor).total_seconds() // 60)
    return anchor + timedelta(minutes=(elapsed // CANDLE_MINUTES) * CANDLE_MINUTES)


def candle_close(start: datetime) -> datetime:
    return start + timedelta(minutes=CANDLE_MINUTES)
