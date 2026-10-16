"""Trading calendar: weekends, holidays, next session."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from orb_intraday_bot.core.clock import IST
from orb_intraday_bot.runtime.calendar import (
    is_trading_day,
    load_holidays,
    next_session_start,
    next_trading_day,
)


def test_weekends_are_not_trading_days() -> None:
    assert not is_trading_day(date(2026, 10, 17), frozenset())  # Saturday
    assert not is_trading_day(date(2026, 10, 18), frozenset())  # Sunday
    assert is_trading_day(date(2026, 10, 16), frozenset())     # Friday


def test_holidays_file_loads_and_skips() -> None:
    holidays = load_holidays()
    assert date(2026, 10, 2) in holidays          # Gandhi Jayanti
    assert not is_trading_day(date(2026, 10, 2), holidays)
    assert next_trading_day(date(2026, 10, 2), holidays) == date(2026, 10, 5)  # Mon after weekend


def test_next_session_start_same_day_before_open() -> None:
    now = datetime(2026, 10, 16, 8, 0, tzinfo=IST)   # Friday morning
    start = next_session_start(now, start="09:00", holidays=frozenset())
    assert start == datetime(2026, 10, 16, 9, 0, tzinfo=IST)


def test_next_session_start_jumps_weekend() -> None:
    now = datetime(2026, 10, 16, 16, 0, tzinfo=IST)  # Friday after close
    start = next_session_start(now, start="09:00", holidays=frozenset())
    assert start.date() == date(2026, 10, 19)        # Monday


def test_next_session_start_mid_day_is_immediate() -> None:
    now = datetime(2026, 10, 16, 11, 0, tzinfo=IST)
    assert next_session_start(now, start="09:00", holidays=frozenset()) == now


def test_missing_holidays_file_is_empty(tmp_path: Path) -> None:
    assert load_holidays(tmp_path / "missing.yaml") == frozenset()
