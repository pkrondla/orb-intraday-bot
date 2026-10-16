"""Trading-day calendar: weekends and NSE holidays."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import yaml

from orb_intraday_bot.core.clock import IST

_DEFAULT_HOLIDAYS = Path(__file__).resolve().parents[3] / "config" / "holidays.yaml"


def load_holidays(path: Path | None = None) -> frozenset[date]:
    target = path or _DEFAULT_HOLIDAYS
    if not target.exists():
        return frozenset()
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    days: set[date] = set()
    for item in raw.get("holidays") or []:
        days.add(date.fromisoformat(str(item)))
    return frozenset(days)


def is_trading_day(day: date, holidays: frozenset[date] | None = None) -> bool:
    if day.weekday() >= 5:          # Saturday / Sunday
        return False
    closed = holidays if holidays is not None else load_holidays()
    return day not in closed


def next_trading_day(day: date, holidays: frozenset[date] | None = None) -> date:
    closed = holidays if holidays is not None else load_holidays()
    candidate = day
    for _ in range(366):
        if is_trading_day(candidate, closed):
            return candidate
        candidate += timedelta(days=1)
    raise RuntimeError(f"no trading day found within a year of {day}")


def session_start_at(day: date, start: str = "09:00") -> datetime:
    h, m = (int(x) for x in start.split(":"))
    return datetime.combine(day, time(h, m), tzinfo=IST)


def next_session_start(
    now: datetime,
    *,
    start: str = "09:00",
    end: str = "15:30",
    holidays: frozenset[date] | None = None,
) -> datetime:
    """When the serve loop should begin (or resume) a session.

    - Before today's start on a trading day: wait until start.
    - During today's session window: return `now` (start immediately; covers mid-day restarts).
    - After today's end, or on a non-trading day: the next trading day's start.
    """
    closed = holidays if holidays is not None else load_holidays()
    today = now.astimezone(IST).date()
    today_start = session_start_at(today, start)
    today_end = session_start_at(today, end)
    if is_trading_day(today, closed):
        if now < today_start:
            return today_start
        if now <= today_end:
            return now
    nxt = next_trading_day(today + timedelta(days=1), closed)
    return session_start_at(nxt, start)
