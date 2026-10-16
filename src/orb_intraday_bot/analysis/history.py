"""A small on-disk cache of real futures candles, so a replay or a tuning run is fast and offline.

The cache is one JSON file per contract in `DATA_DIR/history/`. It remembers which date range it
was fetched for, so a holiday inside the range is not fetched again every time. The current day
is never treated as complete.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from orb_intraday_bot.core.clock import IST
from orb_intraday_bot.core.models import Candle

PIVOT_LOOKBACK_DAYS = 10            # enough to include the previous session across holidays

Fetcher = Callable[[datetime, datetime], list[Candle]]


@dataclass(slots=True)
class History:
    candles: list[Candle]
    first: date | None = None       # the date range this cache is known to cover
    last: date | None = None

    def covers(self, first: date, last: date) -> bool:
        return (
            self.first is not None
            and self.last is not None
            and self.first <= first
            and last <= self.last
        )


def _encode(candle: Candle) -> list[object]:
    return [
        candle.start.isoformat(), candle.open, candle.high, candle.low, candle.close,
        candle.volume,
    ]


def _decode(raw: list[object]) -> Candle:
    return Candle(
        start=datetime.fromisoformat(str(raw[0])),
        open=float(str(raw[1])),
        high=float(str(raw[2])),
        low=float(str(raw[3])),
        close=float(str(raw[4])),
        volume=float(str(raw[5])),
    )


def load_cache(path: Path) -> History:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return History(
            candles=[_decode(c) for c in raw["candles"]],
            first=date.fromisoformat(raw["first"]) if raw.get("first") else None,
            last=date.fromisoformat(raw["last"]) if raw.get("last") else None,
        )
    except (OSError, ValueError, KeyError, TypeError):
        return History([])


def save_cache(path: Path, history: History) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "first": history.first.isoformat() if history.first else None,
        "last": history.last.isoformat() if history.last else None,
        "candles": [_encode(c) for c in history.candles],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def load_history(
    path: Path,
    fetch: Fetcher | None,
    first: date,
    last: date,
    today: date,
    refresh: bool = False,
) -> History:
    """Candles from `first - 10 days` to `last`. `fetch=None` means offline (cache only)."""
    need_first = first - timedelta(days=PIVOT_LOOKBACK_DAYS)
    history = load_cache(path)
    complete_to = min(last, today - timedelta(days=1))
    if not refresh and history.covers(need_first, complete_to) and last < today:
        return history
    if fetch is None:
        return history
    fresh = fetch(
        datetime.combine(need_first, time(9, 0), tzinfo=IST),
        datetime.combine(last + timedelta(days=1), time(0, 0), tzinfo=IST),
    )
    by_start = {c.start: c for c in fresh}
    new_first, new_last = need_first, complete_to
    overlaps = (
        history.first is not None
        and history.last is not None
        and history.first <= complete_to + timedelta(days=1)
        and need_first <= history.last + timedelta(days=1)
    )
    if overlaps:
        assert history.first is not None and history.last is not None
        for c in history.candles:
            by_start.setdefault(c.start, c)
        new_first = min(need_first, history.first)
        new_last = max(complete_to, history.last)
    merged = History(sorted(by_start.values(), key=lambda c: c.start), new_first, new_last)
    save_cache(path, merged)
    return merged
