"""Option expiry handling. Expiry dates always come from the API, never from a weekday rule
(the 19 Oct 2026 weekly is a Monday because of a holiday)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

from orb_intraday_bot.data.contract import parse_date


class NoExpiryError(RuntimeError):
    pass


def parse_expiries(response: Any) -> list[date]:
    """Groww returns {"expiries": ["2026-10-06", ...]} and includes past dates."""
    items = response.get("expiries", []) if isinstance(response, dict) else []
    return sorted({parse_date(x) for x in items})


def collect_expiries(fetch: Callable[[int, int], Any], today: date) -> list[date]:
    """Expiries for the current month and the next, so month-end lookups never come up empty."""
    year, month = today.year, today.month
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    found = set(parse_expiries(fetch(year, month))) | set(parse_expiries(fetch(ny, nm)))
    return sorted(found)


def choose_option_expiry(expiries: list[date], today: date) -> date:
    """The nearest weekly expiry that is strictly after today (an expiry today rolls over)."""
    upcoming = [e for e in expiries if e > today]
    if not upcoming:
        raise NoExpiryError(f"No option expiry after {today}")
    return min(upcoming)
