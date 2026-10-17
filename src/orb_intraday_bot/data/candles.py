"""Parsing and cleaning of Groww 3-minute candle rows.

Verified against the live API (Oct 2026):
- A row is [time, open, high, low, close, volume, oi]. `time` is the candle START, IST, with no
  timezone suffix. `oi` is None for the current day.
- The last row can be the still-forming candle.
- Past days also contain pre-open (09:00-09:12) and post-close (15:30-15:39) candles.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

from orb_intraday_bot.core.clock import CANDLE_MINUTES, IST
from orb_intraday_bot.core.models import Candle
from orb_intraday_bot.data.session import in_session


class CandleDataError(ValueError):
    """The API returned a candle row we cannot trust."""


def parse_timestamp(raw: str) -> datetime:
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise CandleDataError(f"Bad candle timestamp: {raw!r}") from exc
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def _num(value: Any, field: str, row: Sequence[Any]) -> float:
    if value is None:
        raise CandleDataError(f"Missing {field} in candle row {list(row)}")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise CandleDataError(f"Bad {field} in candle row {list(row)}") from exc


def parse_candle_rows(
    rows: Iterable[Sequence[Any]],
    *,
    now: datetime | None = None,
    session_only: bool = True,
) -> list[Candle]:
    """Convert raw rows to Candles: ordered, de-duplicated, closed, regular session only.

    `now` is the current time; any candle that has not closed by then is dropped. Pass None
    (replay of old data) to keep every candle.
    """
    by_start: dict[datetime, Candle] = {}
    for row in rows:
        if len(row) < 5:
            raise CandleDataError(f"Candle row too short: {list(row)}")
        start = parse_timestamp(str(row[0]))
        if session_only and not in_session(start):
            continue
        if now is not None and start + timedelta(minutes=CANDLE_MINUTES) > now:
            continue
        volume = float(row[5]) if len(row) > 5 and row[5] is not None else 0.0
        oi = float(row[6]) if len(row) > 6 and row[6] is not None else None
        try:
            candle = Candle(
                start=start,
                open=_num(row[1], "open", row),
                high=_num(row[2], "high", row),
                low=_num(row[3], "low", row),
                close=_num(row[4], "close", row),
                volume=volume,
                oi=oi,
            )
        except ValueError as exc:
            raise CandleDataError(str(exc)) from exc
        by_start[start] = candle  # a later duplicate replaces an earlier one
    return [by_start[k] for k in sorted(by_start)]
