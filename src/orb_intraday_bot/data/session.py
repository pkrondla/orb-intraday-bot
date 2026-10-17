"""NSE regular-session helpers for 3-minute candles."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from orb_intraday_bot.core.clock import CANDLE_MINUTES, IST, MARKET_CLOSE, MARKET_OPEN

# The last regular-session candle starts at 15:27 and closes at 15:30.
LAST_CANDLE_START = time(15, 27)


def in_session(start: datetime) -> bool:
    """True if a candle starting at `start` belongs to the 09:15-15:30 regular session."""
    t = start.astimezone(IST).time()
    return MARKET_OPEN <= t <= LAST_CANDLE_START


def session_candle_starts(day: date) -> list[datetime]:
    """All 125 candle start times of a regular session."""
    first = datetime.combine(day, MARKET_OPEN, tzinfo=IST)
    end = datetime.combine(day, MARKET_CLOSE, tzinfo=IST)
    out: list[datetime] = []
    cur = first
    while cur < end:
        out.append(cur)
        cur += timedelta(minutes=CANDLE_MINUTES)
    return out
