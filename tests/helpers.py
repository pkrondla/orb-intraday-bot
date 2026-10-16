"""Test helpers for building candles."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from orb_intraday_bot.core.clock import IST
from orb_intraday_bot.core.models import Candle

DAY = date(2026, 10, 15)


def at(hh: int, mm: int, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST)


def candle(
    start: datetime,
    o: float,
    h: float,
    low: float,
    c: float,
    volume: float = 1000.0,
) -> Candle:
    return Candle(start=start, open=o, high=h, low=low, close=c, volume=volume)


def series(
    specs: list[tuple[float, float, float, float]],
    first: datetime | None = None,
) -> list[Candle]:
    """Build consecutive 3-minute candles from (open, high, low, close) tuples."""
    start = first or at(9, 15)
    return [
        candle(start + timedelta(minutes=3 * i), o, h, low, c)
        for i, (o, h, low, c) in enumerate(specs)
    ]
