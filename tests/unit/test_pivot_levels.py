"""Pivot inputs that match TradingView: daily high/low plus the exchange's closing price.

TradingView's daily bar includes the pre-open trade (so its low can be lower than any 3-minute
candle) and closes on the exchange's closing price, not the last candle. Groww gives the high and
low in its daily bar and the previous close in its live quote; a closing VWAP built from 1-minute
candles stands in for days the quote can no longer answer.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from orb_intraday_bot.adapters.groww.market_data import GrowwMarketData
from orb_intraday_bot.analysis.backtest import run_day, run_range
from orb_intraday_bot.analysis.levels import (
    LevelsCache,
    levels_path,
    load_cache,
    load_range,
    remember,
    save_cache,
)
from orb_intraday_bot.config.settings import InstrumentConfig, StrategyConfig
from orb_intraday_bot.core.clock import FixedClock
from orb_intraday_bot.indicators.pivots import (
    CLOSE_CANDLE,
    CLOSE_EXCHANGE,
    CLOSE_VWAP,
    SessionLevels,
    better_levels,
    levels_from_candles,
    traditional_pivots,
)
from orb_intraday_bot.runtime.day import TradingDay
from tests.helpers import at, series

PREV = date(2026, 10, 14)
TODAY = date(2026, 10, 15)

INSTRUMENTS = pd.DataFrame([{
    "underlying_symbol": "NIFTY", "instrument_type": "FUT", "expiry_date": "2026-10-27",
    "trading_symbol": "NIFTY26OCTFUT", "groww_symbol": "NSE-NIFTY-27Oct26-FUT",
    "exchange_token": "1", "lot_size": "65", "tick_size": "0.1",
}])


class LevelsClient:
    """Groww client stand-in with 3-minute, 1-minute and daily candles and a quote."""

    CANDLE_INTERVAL_MIN_1 = "1minute"
    CANDLE_INTERVAL_MIN_3 = "3minute"
    CANDLE_INTERVAL_DAY = "1day"

    def __init__(self) -> None:
        self.quote: dict[str, Any] = {
            "ohlc": {"close": 22984.8}, "last_price": 23102.0, "day_change": 117.2,
        }
        self.candles: dict[str, list[list[Any]]] = {
            "3minute": [
                ["2026-10-14T09:15:00", 22680.0, 23090.0, 22675.0, 22700.0, 10, None],
                ["2026-10-14T15:27:00", 22970.0, 22999.0, 22970.0, 22998.8, 10, None],
            ],
            # The daily low includes a pre-open trade the 3-minute data never shows.
            "1day": [["2026-10-14T00:00:00", None, 23095.0, 22671.0, 22993.2, 99, 1]],
            "1minute": [
                ["2026-10-14T14:59:00", 1.0, 1.0, 1.0, 1.0, 1000, None],      # before the window
                ["2026-10-14T15:00:00", 22980.0, 22980.0, 22980.0, 22980.0, 100, None],
                ["2026-10-14T15:01:00", 22990.0, 22990.0, 22990.0, 22990.0, 300, None],
                ["2026-10-14T15:40:00", 1.0, 1.0, 1.0, 1.0, 1000, None],      # after the window
            ],
        }
        self.broken: set[str] = set()
        self.requests: list[str] = []

    def get_all_instruments(self) -> pd.DataFrame:
        return INSTRUMENTS

    def get_historical_candles(self, **kw: Any) -> dict[str, Any]:
        interval = kw["candle_interval"]
        self.requests.append(interval)
        if interval in self.broken:
            raise ConnectionError(interval)
        return {"candles": self.candles[interval]}

    def get_quote(self, **kw: Any) -> dict[str, Any]:
        self.requests.append("quote")
        if "quote" in self.broken:
            raise ConnectionError("quote")
        return self.quote


def make_md(now: Any = None) -> tuple[GrowwMarketData, LevelsClient]:
    client = LevelsClient()
    md = GrowwMarketData(client, InstrumentConfig(), FixedClock(now or at(9, 10, TODAY)))
    return md, client


def levels_for(md: GrowwMarketData) -> SessionLevels:
    return md.previous_session_levels(md.futures_contract(TODAY), TODAY)


# -- the value types ---------------------------------------------------------------------------
def test_levels_pivots_are_the_traditional_formula() -> None:
    lv = SessionLevels(PREV, 23095.0, 22671.0, 22984.8, CLOSE_EXCHANGE)
    assert lv.pivots() == traditional_pivots(23095.0, 22671.0, 22984.8)
    assert lv.pivots().p == pytest.approx(22916.93, abs=0.01)    # TradingView shows 22916.9


def test_levels_from_candles_is_the_fallback_close() -> None:
    candles = series([(10, 12, 9, 11), (11, 14, 10, 13)], first=at(9, 15, PREV))
    assert levels_from_candles(PREV, candles) == SessionLevels(PREV, 14, 9, 13, CLOSE_CANDLE)


def test_better_levels_prefers_the_more_trustworthy_close() -> None:
    candle = SessionLevels(PREV, 3, 1, 2, CLOSE_CANDLE)
    vwap = SessionLevels(PREV, 3, 1, 2.5, CLOSE_VWAP)
    exchange = SessionLevels(PREV, 3, 1, 2.4, CLOSE_EXCHANGE)
    assert better_levels(None, candle) is candle
    assert better_levels(candle, vwap) is vwap
    assert better_levels(vwap, exchange) is exchange
    assert better_levels(exchange, vwap) is exchange        # a later, weaker reading never wins
    assert better_levels(vwap, candle) is vwap


def test_describe_names_the_source() -> None:
    text = SessionLevels(PREV, 23095.0, 22671.0, 22984.8, CLOSE_EXCHANGE).describe()
    assert text == "H 23095.0 L 22671.0 C 22984.8 (exchange)"


# -- the Groww adapter ---------------------------------------------------------------------------
def test_live_morning_uses_exchange_close_and_daily_high_low() -> None:
    md, _ = make_md()
    assert levels_for(md) == SessionLevels(PREV, 23095.0, 22671.0, 22984.8, CLOSE_EXCHANGE)


def test_after_the_session_the_quote_is_not_used() -> None:
    md, client = make_md(at(16, 0, TODAY))              # today's close may now be in the quote
    lv = levels_for(md)
    assert lv.close_source == CLOSE_VWAP
    assert "quote" not in client.requests


def test_vwap_is_volume_weighted_and_limited_to_the_closing_window() -> None:
    md, _ = make_md()
    vwap = md.closing_vwap(md.futures_contract(TODAY), PREV)
    assert vwap == pytest.approx((22980 * 100 + 22990 * 300) / 400)


def test_vwap_is_none_without_closing_candles() -> None:
    md, client = make_md()
    client.candles["1minute"] = []
    assert md.closing_vwap(md.futures_contract(TODAY), PREV) is None


@pytest.mark.parametrize(
    "quote",
    [
        {"ohlc": {"close": 22984.8}, "last_price": 23102.0, "day_change": 50.0},   # disagrees
        {"ohlc": {"close": None}, "last_price": 23102.0, "day_change": 117.2},
        {"ohlc": {"close": 0}},
        {},
    ],
)
def test_untrustworthy_quote_falls_back_to_the_vwap(quote: dict[str, Any]) -> None:
    md, client = make_md()
    client.quote = quote
    lv = levels_for(md)
    assert lv.close_source == CLOSE_VWAP
    assert lv.close == pytest.approx(22987.5)


def test_quote_outside_the_days_range_is_ignored() -> None:
    md, client = make_md()
    client.quote = {"ohlc": {"close": 21000.0}}
    assert levels_for(md).close_source == CLOSE_VWAP


def test_quote_without_day_change_is_accepted_when_in_range() -> None:
    md, client = make_md()
    client.quote = {"ohlc": {"close": 22984.8}}
    assert levels_for(md).close_source == CLOSE_EXCHANGE


@pytest.mark.parametrize("broken", [{"quote"}, {"quote", "1minute"}, {"quote", "1minute", "1day"}])
def test_every_lookup_failure_falls_back_without_raising(broken: set[str]) -> None:
    md, client = make_md()
    client.broken = broken
    lv = levels_for(md)
    assert lv.day == PREV
    if "1minute" in broken:
        assert (lv.close, lv.close_source) == (22998.8, CLOSE_CANDLE)
    if "1day" in broken:
        assert (lv.high, lv.low) == (23090.0, 22675.0)          # the candles' own range


def test_daily_bar_that_does_not_contain_the_candles_is_ignored() -> None:
    md, client = make_md()
    client.candles["1day"] = [["2026-10-14T00:00:00", None, 22800.0, 22700.0, 22750.0, 1, 1]]
    lv = levels_for(md)
    assert (lv.high, lv.low) == (23090.0, 22675.0)


def test_historical_levels_skip_today_and_days_without_a_vwap() -> None:
    md, client = make_md()
    client.candles["1day"] = [
        ["2026-10-13T00:00:00", None, 23000.0, 22600.0, 22900.0, 1, 1],
        ["2026-10-14T00:00:00", None, 23095.0, 22671.0, 22993.2, 1, 1],
        ["2026-10-15T00:00:00", None, 23100.0, 22900.0, 23000.0, 1, 1],       # today: unfinished
    ]
    got = md.historical_levels(md.futures_contract(TODAY), date(2026, 10, 13), TODAY)
    # The fake returns the same 1-minute rows for every day, so only 14 Oct has a window.
    assert list(got) == [PREV]
    assert got[PREV].close_source == CLOSE_VWAP
    assert got[PREV].close == pytest.approx(22987.5)


# -- the cache -------------------------------------------------------------------------------------
def test_cache_round_trip_and_range(tmp_path: Path) -> None:
    path = levels_path(tmp_path / "history" / "NIFTY26OCTFUT.json")
    assert path.name == "NIFTY26OCTFUT.levels.json"
    cache = LevelsCache({PREV: SessionLevels(PREV, 3, 1, 2, CLOSE_VWAP)}, PREV, TODAY)
    save_cache(path, cache)
    loaded = load_cache(path)
    assert loaded.days == cache.days
    assert loaded.covers(PREV, TODAY) and not loaded.covers(date(2026, 10, 1), TODAY)


def test_unreadable_cache_is_empty(tmp_path: Path) -> None:
    bad = tmp_path / "x.levels.json"
    bad.write_text("not json", encoding="utf-8")
    assert load_cache(bad) == LevelsCache()
    assert load_cache(tmp_path / "missing.json") == LevelsCache()


def test_remember_keeps_the_exchange_reading_over_a_later_vwap(tmp_path: Path) -> None:
    path = tmp_path / "a.levels.json"
    remember(path, SessionLevels(PREV, 3, 1, 2.0, CLOSE_EXCHANGE))
    remember(path, SessionLevels(PREV, 3, 1, 2.5, CLOSE_VWAP))
    assert load_cache(path).days[PREV].close == 2.0


def test_load_range_fetches_once_then_serves_from_the_file(tmp_path: Path) -> None:
    path = tmp_path / "a.levels.json"
    calls: list[tuple[date, date]] = []

    def fetch(first: date, last: date) -> dict[date, SessionLevels]:
        calls.append((first, last))
        return {PREV: SessionLevels(PREV, 3, 1, 2, CLOSE_VWAP)}

    first = date(2026, 10, 5)
    assert load_range(path, fetch, first, TODAY, TODAY)[PREV].close == 2
    assert load_range(path, fetch, first, TODAY, TODAY)[PREV].close == 2
    assert calls == [(first, TODAY)]                                 # second call: cache hit
    load_range(path, fetch, first, TODAY, TODAY, refresh=True)
    assert len(calls) == 2


def test_load_range_does_not_claim_a_gap_it_never_fetched(tmp_path: Path) -> None:
    path = tmp_path / "a.levels.json"
    calls: list[date] = []

    def fetch(first: date, last: date) -> dict[date, SessionLevels]:
        calls.append(first)
        return {}

    load_range(path, fetch, date(2026, 9, 1), date(2026, 9, 10), TODAY)
    load_range(path, fetch, date(2026, 9, 3), date(2026, 9, 8), TODAY)      # inside: no fetch
    assert calls == [date(2026, 9, 1)]
    load_range(path, fetch, date(2026, 10, 1), date(2026, 10, 10), TODAY)   # not adjacent
    cache = load_cache(path)
    assert cache.covers(date(2026, 10, 1), date(2026, 10, 10))
    assert not cache.covers(date(2026, 9, 1), date(2026, 10, 10))           # the gap is not claimed


def test_load_range_survives_a_failed_fetch_and_works_offline(tmp_path: Path) -> None:
    path = tmp_path / "a.levels.json"
    remember(path, SessionLevels(PREV, 3, 1, 2, CLOSE_EXCHANGE))

    def broken(first: date, last: date) -> dict[date, SessionLevels]:
        raise ConnectionError("down")

    assert PREV in load_range(path, broken, date(2026, 10, 1), TODAY, TODAY)
    assert PREV in load_range(path, None, date(2026, 10, 1), TODAY, TODAY)


# -- the replay uses the levels --------------------------------------------------------------------
def two_sessions() -> list[Any]:
    first = series([(100, 110, 90, 100)] * 125, first=at(9, 15, PREV))
    second = series([(100, 105, 95, 100)] * 125, first=at(9, 15, TODAY))
    return first + second


def spy_on_pivots(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record the pivots every `TradingDay` built by the backtest is given."""
    seen: list[Any] = []

    def spy(cfg: Any, day: Any, symbol: Any, pivots: Any, *rest: Any) -> TradingDay:
        seen.append(pivots)
        return TradingDay(cfg, day, symbol, pivots, *rest)

    monkeypatch.setattr("orb_intraday_bot.analysis.backtest.TradingDay", spy)
    return seen


def test_run_day_takes_pivots_from_the_levels_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = spy_on_pivots(monkeypatch)
    cfg = StrategyConfig()
    candles = two_sessions()
    previous, today = candles[:125], candles[125:]
    levels = SessionLevels(PREV, 120.0, 80.0, 96.0, CLOSE_EXCHANGE)

    run_day(cfg, TODAY, "S", 65, previous, today)
    run_day(cfg, TODAY, "S", 65, previous, today, levels=levels)
    assert seen[0] == traditional_pivots(110, 90, 100)       # from the candles
    assert seen[1] == traditional_pivots(120, 80, 96)        # from the levels


def test_run_range_picks_the_levels_of_the_previous_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = spy_on_pivots(monkeypatch)
    levels = {PREV: SessionLevels(PREV, 120.0, 80.0, 96.0, CLOSE_VWAP)}
    run_range(StrategyConfig(), two_sessions(), "S", 65, levels=levels)
    run_range(StrategyConfig(), two_sessions(), "S", 65, levels={})
    assert seen == [traditional_pivots(120, 80, 96), traditional_pivots(110, 90, 100)]
