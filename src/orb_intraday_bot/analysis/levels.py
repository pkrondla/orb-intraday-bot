"""A small on-disk record of each session's pivot inputs (high, low, close, close source).

One JSON file per contract in `DATA_DIR/history/`. The live run saves the exchange's reported close
every morning (the number is only available then), and replay reads it back, so a later replay of
that day uses the same pivots the live run used. For days the live run never saw, replay fetches
the closing-VWAP version once and saves it. The exchange reading always wins over the VWAP one.

Like the candle cache, the file remembers which date range was fetched, so a holiday inside the
range is not fetched again every time.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from orb_intraday_bot.indicators.pivots import SessionLevels, better_levels

Fetcher = Callable[[date, date], Mapping[date, SessionLevels]]


@dataclass(slots=True)
class LevelsCache:
    days: dict[date, SessionLevels] = field(default_factory=dict)
    first: date | None = None       # the date range a fetch is known to have covered
    last: date | None = None

    def covers(self, first: date, last: date) -> bool:
        return (
            self.first is not None
            and self.last is not None
            and self.first <= first
            and last <= self.last
        )

    def add(self, new: Mapping[date, SessionLevels]) -> None:
        for day, levels in new.items():
            self.days[day] = better_levels(self.days.get(day), levels)


def levels_path(history_path: Path) -> Path:
    """`history/NIFTY26OCTFUT.json` -> `history/NIFTY26OCTFUT.levels.json`."""
    return history_path.with_suffix(".levels.json")


def load_cache(path: Path) -> LevelsCache:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        days = {
            date.fromisoformat(day): SessionLevels(
                date.fromisoformat(day),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                str(row["source"]),
            )
            for day, row in raw["days"].items()
        }
        return LevelsCache(
            days,
            date.fromisoformat(raw["first"]) if raw.get("first") else None,
            date.fromisoformat(raw["last"]) if raw.get("last") else None,
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return LevelsCache()


def save_cache(path: Path, cache: LevelsCache) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "first": cache.first.isoformat() if cache.first else None,
        "last": cache.last.isoformat() if cache.last else None,
        "days": {
            day.isoformat(): {
                "high": lv.high, "low": lv.low, "close": lv.close, "source": lv.close_source,
            }
            for day, lv in sorted(cache.days.items())
        },
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    tmp.replace(path)


def remember(path: Path, levels: SessionLevels) -> None:
    """Add one session's levels to the file (the live run calls this each morning)."""
    cache = load_cache(path)
    cache.add({levels.day: levels})
    save_cache(path, cache)


def load_range(
    path: Path,
    fetch: Fetcher | None,
    first: date,
    last: date,
    today: date,
    refresh: bool = False,
) -> dict[date, SessionLevels]:
    """Saved levels for `first..last`, fetching the sessions that are not covered yet.

    A failed fetch is not an error: replay then uses the candles for those days.
    """
    cache = load_cache(path)
    complete_to = min(last, today - timedelta(days=1))
    if fetch is not None and (refresh or not cache.covers(first, complete_to)):
        try:
            cache.add(fetch(first, last))
        except Exception:
            return dict(cache.days)
        one = timedelta(days=1)
        overlaps = (
            cache.first is not None
            and cache.last is not None
            and cache.first <= complete_to + one
            and first <= cache.last + one
        )
        if overlaps:
            assert cache.first is not None and cache.last is not None
            cache.first, cache.last = min(cache.first, first), max(cache.last, complete_to)
        else:                       # a gap would claim days that were never fetched
            cache.first, cache.last = first, complete_to
        save_cache(path, cache)
    return dict(cache.days)
