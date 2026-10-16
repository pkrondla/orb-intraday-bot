from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import pytest

from orb_intraday_bot.adapters.groww.market_data import GrowwMarketData, NoSessionDataError
from orb_intraday_bot.config.settings import InstrumentConfig
from orb_intraday_bot.core.clock import FixedClock
from orb_intraday_bot.data.contract import NoContractError, select_futures_contract
from orb_intraday_bot.data.expiries import (
    NoExpiryError,
    choose_option_expiry,
    collect_expiries,
    parse_expiries,
)
from tests.helpers import at


def fut(expiry: str, symbol: str, underlying: str = "NIFTY") -> dict[str, Any]:
    return {
        "underlying_symbol": underlying, "instrument_type": "FUT", "expiry_date": expiry,
        "trading_symbol": symbol, "groww_symbol": f"NSE-{underlying}-{expiry}-FUT",
        "exchange_token": "48704", "lot_size": "65", "tick_size": "0.1",
    }


ROWS = [
    fut("2026-12-29", "NIFTY26DECFUT"),
    fut("2026-10-27", "NIFTY26OCTFUT"),
    fut("2026-11-23", "NIFTY26NOVFUT"),
    fut("2026-10-27", "BANKNIFTY26OCTFUT", underlying="BANKNIFTY"),
    {"underlying_symbol": "NIFTY", "instrument_type": "CE", "expiry_date": "2026-10-19",
     "trading_symbol": "NIFTY26O1923000CE", "groww_symbol": "x", "exchange_token": "1",
     "lot_size": "65", "tick_size": "0.05"},
]


def test_selects_current_month_contract() -> None:
    c = select_futures_contract(ROWS, date(2026, 10, 15))
    assert c.trading_symbol == "NIFTY26OCTFUT"
    assert c.expiry == date(2026, 10, 27)
    assert c.lot_size == 65
    assert c.tick_size == 0.1


def test_stays_on_contract_through_expiry_day() -> None:
    assert select_futures_contract(ROWS, date(2026, 10, 27)).trading_symbol == "NIFTY26OCTFUT"


def test_rolls_on_first_session_after_expiry() -> None:
    assert select_futures_contract(ROWS, date(2026, 10, 28)).trading_symbol == "NIFTY26NOVFUT"


def test_roll_days_before_expiry() -> None:
    # N=1: roll on the expiry day itself. N=3: roll once fewer than 3 days remain.
    assert select_futures_contract(
        ROWS, date(2026, 10, 27), roll_days_before_expiry=1
    ).trading_symbol == "NIFTY26NOVFUT"
    assert select_futures_contract(
        ROWS, date(2026, 10, 25), roll_days_before_expiry=3
    ).trading_symbol == "NIFTY26NOVFUT"
    assert select_futures_contract(
        ROWS, date(2026, 10, 24), roll_days_before_expiry=3
    ).trading_symbol == "NIFTY26OCTFUT"


def test_no_contract_raises() -> None:
    with pytest.raises(NoContractError):
        select_futures_contract(ROWS, date(2027, 1, 5))


EXPIRIES = {"expiries": ["2026-10-06", "2026-10-13", "2026-10-19", "2026-10-27"]}


def test_parse_expiries_sorted_unique() -> None:
    parsed = parse_expiries({"expiries": ["2026-10-27", "2026-10-06", "2026-10-06"]})
    assert parsed == [date(2026, 10, 6), date(2026, 10, 27)]
    assert parse_expiries(None) == []


def test_nearest_expiry_skips_past_and_handles_monday_holiday_expiry() -> None:
    expiries = parse_expiries(EXPIRIES)
    assert choose_option_expiry(expiries, date(2026, 10, 15)) == date(2026, 10, 19)  # a Monday


def test_expiry_today_rolls_to_next() -> None:
    expiries = parse_expiries(EXPIRIES)
    assert choose_option_expiry(expiries, date(2026, 10, 19)) == date(2026, 10, 27)


def test_no_upcoming_expiry_raises() -> None:
    with pytest.raises(NoExpiryError):
        choose_option_expiry(parse_expiries(EXPIRIES), date(2026, 10, 27))


def test_collect_expiries_includes_next_month_at_month_end() -> None:
    by_month = {(2026, 10): EXPIRIES, (2026, 11): {"expiries": ["2026-11-03", "2026-11-10"]}}
    expiries = collect_expiries(lambda y, m: by_month.get((y, m), {"expiries": []}),
                                date(2026, 10, 28))
    assert choose_option_expiry(expiries, date(2026, 10, 28)) == date(2026, 11, 3)


def test_collect_expiries_december_rolls_year() -> None:
    seen: list[tuple[int, int]] = []

    def fetch(y: int, m: int) -> Any:
        seen.append((y, m))
        return {"expiries": []}

    collect_expiries(fetch, date(2026, 12, 20))
    assert seen == [(2026, 12), (2027, 1)]


# -- market data adapter, with a fake client -------------------------------------------------
class FakeClient:
    CANDLE_INTERVAL_MIN_3 = "3minute"

    def __init__(self) -> None:
        self.candle_requests: list[tuple[str, str]] = []

    def get_all_instruments(self) -> pd.DataFrame:
        return pd.DataFrame(ROWS + [fut("2026-10-27", "RELIANCE26OCTFUT", underlying="RELIANCE")])

    def get_historical_candles(self, **kw: Any) -> dict[str, Any]:
        self.candle_requests.append((kw["start_time"], kw["end_time"]))
        return {"candles": [
            ["2026-10-13T09:12:00", 1.0, 2.0, 0.5, 1.5, 5, None],     # pre-open
            ["2026-10-13T09:15:00", 100.0, 105.0, 99.0, 104.0, 10, None],
            ["2026-10-13T15:27:00", 104.0, 106.0, 103.0, 105.0, 10, None],
            ["2026-10-14T09:15:00", 105.0, 110.0, 104.0, 108.0, 10, None],
            ["2026-10-14T15:27:00", 108.0, 112.0, 107.0, 111.0, 10, None],
            ["2026-10-14T15:30:00", 111.0, 111.0, 110.0, 110.0, 1, None],   # post-close
        ]}

    def get_expiries(self, **kw: Any) -> dict[str, Any]:
        return EXPIRIES if (kw["year"], kw["month"]) == (2026, 10) else {"expiries": []}

    def get_ltp(self, **kw: Any) -> dict[str, float]:
        return {"NSE_NIFTY26OCTFUT": 22998.5, "NSE_NIFTY26O1923000CE": 113.05}


def make_md() -> tuple[GrowwMarketData, FakeClient]:
    client = FakeClient()
    clock = FixedClock(at(12, 0))
    return GrowwMarketData(client, InstrumentConfig(), clock), client


def test_adapter_filters_to_underlying_and_selects_contract() -> None:
    md, _ = make_md()
    assert {r["underlying_symbol"] for r in md.instrument_rows()} == {"NIFTY", "BANKNIFTY"} \
        or {r["underlying_symbol"] for r in md.instrument_rows()} == {"NIFTY"}
    assert md.futures_contract().trading_symbol == "NIFTY26OCTFUT"


def test_previous_session_picks_latest_day_before_today() -> None:
    md, _ = make_md()
    contract = md.futures_contract()
    day, candles = md.previous_session(contract, date(2026, 10, 15))
    assert day == date(2026, 10, 14)
    assert [c.start.strftime("%H:%M") for c in candles] == ["09:15", "15:27"]


def test_previous_session_without_data_raises() -> None:
    md, client = make_md()
    client.get_historical_candles = lambda **kw: {"candles": []}  # type: ignore[method-assign]
    with pytest.raises(NoSessionDataError):
        md.previous_session(md.futures_contract(), date(2026, 10, 15))


def test_long_ranges_are_chunked() -> None:
    md, client = make_md()
    contract = md.futures_contract()
    md.fetch_candles(contract, at(9, 0, date(2026, 8, 1)), at(15, 45, date(2026, 10, 1)))
    assert len(client.candle_requests) == 3     # ~61 days in chunks of 28


def test_nearest_option_expiry_and_ltp() -> None:
    md, _ = make_md()
    assert md.nearest_option_expiry(date(2026, 10, 15)) == date(2026, 10, 19)
    assert md.ltp(["NIFTY26OCTFUT", "NIFTY26O1923000CE"]) == {
        "NIFTY26OCTFUT": 22998.5, "NIFTY26O1923000CE": 113.05,
    }
