from __future__ import annotations

from datetime import date

import pytest

from orb_intraday_bot.data.candles import CandleDataError, parse_candle_rows
from orb_intraday_bot.data.session import in_session, session_candle_starts
from tests.helpers import at

# Rows copied in shape from the live API (Oct 2026): [time, o, h, l, c, volume, oi]
ROWS = [
    ["2026-10-14T09:12:00", 22400.0, 22410.0, 22395.0, 22405.0, 100, None],   # pre-open
    ["2026-10-14T09:15:00", 22405.0, 22420.0, 22400.0, 22415.0, 9000, 570401],
    ["2026-10-14T09:18:00", 22415.0, 22425.0, 22410.0, 22420.0, 8000, 570423],
    ["2026-10-14T15:27:00", 22430.0, 22435.0, 22425.0, 22428.0, 7000, 570388],
    ["2026-10-14T15:30:00", 22428.0, 22429.0, 22420.0, 22425.0, 50, None],    # post-close
    ["2026-10-14T15:39:00", 22425.0, 22426.0, 22424.0, 22425.0, 10, None],    # post-close
]


def test_session_filter_drops_pre_open_and_post_close() -> None:
    candles = parse_candle_rows(ROWS)
    assert [c.start.strftime("%H:%M") for c in candles] == ["09:15", "09:18", "15:27"]


def test_timestamps_are_ist_aware() -> None:
    candles = parse_candle_rows(ROWS)
    assert candles[0].start.utcoffset().total_seconds() == 5.5 * 3600  # type: ignore[union-attr]


def test_oi_none_is_allowed() -> None:
    rows = [["2026-10-15T09:15:00", 1.0, 2.0, 0.5, 1.5, 10, None]]
    assert parse_candle_rows(rows)[0].oi is None


def test_forming_candle_is_dropped() -> None:
    rows = [
        ["2026-10-15T11:36:00", 100.0, 101.0, 99.0, 100.5, 500, None],
        ["2026-10-15T11:39:00", 100.5, 100.8, 100.4, 100.5, 20, None],   # forming at 11:39:06
    ]
    candles = parse_candle_rows(rows, now=at(11, 39))
    assert [c.start.strftime("%H:%M") for c in candles] == ["11:36"]


def test_candle_is_kept_once_closed() -> None:
    rows = [["2026-10-15T11:39:00", 100.5, 100.8, 100.4, 100.5, 20, None]]
    assert len(parse_candle_rows(rows, now=at(11, 42))) == 1
    assert len(parse_candle_rows(rows, now=at(11, 41))) == 0


def test_duplicates_replaced_and_sorted() -> None:
    rows = [
        ["2026-10-15T09:18:00", 2.0, 3.0, 1.0, 2.5, 10, None],
        ["2026-10-15T09:15:00", 1.0, 2.0, 0.5, 1.5, 10, None],
        ["2026-10-15T09:15:00", 1.0, 2.2, 0.5, 1.6, 12, None],   # later duplicate wins
    ]
    candles = parse_candle_rows(rows)
    assert [c.start.strftime("%H:%M") for c in candles] == ["09:15", "09:18"]
    assert candles[0].high == 2.2


def test_inconsistent_candle_raises() -> None:
    rows = [["2026-10-15T09:15:00", 10.0, 9.0, 8.0, 9.5, 10, None]]   # high below open
    with pytest.raises(CandleDataError):
        parse_candle_rows(rows)


def test_malformed_rows_raise() -> None:
    with pytest.raises(CandleDataError):
        parse_candle_rows([["2026-10-15T09:15:00", 1.0]])
    with pytest.raises(CandleDataError):
        parse_candle_rows([["not-a-time", 1.0, 2.0, 0.5, 1.5, 1, None]])
    with pytest.raises(CandleDataError):
        parse_candle_rows([["2026-10-15T09:15:00", None, 2.0, 0.5, 1.5, 1, None]])


def test_session_has_125_candles() -> None:
    starts = session_candle_starts(date(2026, 10, 15))
    assert len(starts) == 125
    assert starts[0].strftime("%H:%M") == "09:15"
    assert starts[-1].strftime("%H:%M") == "15:27"


@pytest.mark.parametrize(
    ("hh", "mm", "expected"),
    [(9, 12, False), (9, 15, True), (15, 27, True), (15, 30, False), (15, 39, False)],
)
def test_in_session_boundaries(hh: int, mm: int, expected: bool) -> None:
    assert in_session(at(hh, mm)) is expected
