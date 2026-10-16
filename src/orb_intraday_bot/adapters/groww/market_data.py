"""Groww implementation of market data access (read-only)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

from orb_intraday_bot.config.settings import InstrumentConfig
from orb_intraday_bot.core.clock import IST, Clock, SystemClock
from orb_intraday_bot.core.errors import DataUnavailableError
from orb_intraday_bot.core.models import Candle
from orb_intraday_bot.data.candles import parse_candle_rows
from orb_intraday_bot.data.contract import FuturesContract, select_futures_contract
from orb_intraday_bot.data.expiries import choose_option_expiry, collect_expiries
from orb_intraday_bot.options.chain import (
    OptionChain,
    OptionContract,
    parse_option_chain,
    parse_option_contracts,
)

_FMT = "%Y-%m-%d %H:%M:%S"
_MAX_DAYS_PER_REQUEST = 28   # Groww allows 30 days of 3-minute candles per request


class NoSessionDataError(DataUnavailableError, RuntimeError):
    pass


class GrowwMarketData:
    def __init__(
        self,
        client: Any,
        instrument: InstrumentConfig,
        clock: Clock | None = None,
    ) -> None:
        self._client = client
        self._inst = instrument
        self._clock = clock or SystemClock()
        self._rows: list[dict[str, Any]] | None = None
        self._contracts: dict[date, dict[str, OptionContract]] = {}

    # -- instruments ---------------------------------------------------------------------
    def _underlying_rows(self) -> list[dict[str, Any]]:
        if self._rows is None:
            df = self._client.get_all_instruments()
            df = df[df["underlying_symbol"].astype(str).str.upper() == self._inst.underlying]
            self._rows = df.to_dict("records")
        return self._rows

    def instrument_rows(self) -> list[dict[str, Any]]:
        """All futures and option rows for the underlying (cached)."""
        return self._underlying_rows()

    def futures_contract(self, today: date | None = None) -> FuturesContract:
        today = today or self._clock.now().date()
        return select_futures_contract(
            self._underlying_rows(),
            today,
            underlying=self._inst.underlying,
            roll_days_before_expiry=self._inst.roll_days_before_expiry,
        )

    # -- candles -------------------------------------------------------------------------
    def fetch_candles(
        self, contract: FuturesContract, start: datetime, end: datetime
    ) -> list[Candle]:
        """Closed regular-session candles between two instants (chunked to the API limit)."""
        now = self._clock.now()
        rows: list[Sequence[Any]] = []
        cursor = start.astimezone(IST)
        stop = end.astimezone(IST)
        while cursor < stop:
            chunk_end = min(cursor + timedelta(days=_MAX_DAYS_PER_REQUEST), stop)
            res = self._client.get_historical_candles(
                exchange=self._inst.exchange,
                segment=self._inst.segment,
                groww_symbol=contract.groww_symbol,
                start_time=cursor.strftime(_FMT),
                end_time=chunk_end.strftime(_FMT),
                candle_interval=self._client.CANDLE_INTERVAL_MIN_3,
            )
            rows.extend(res.get("candles") or [])
            cursor = chunk_end
        return parse_candle_rows(rows, now=now)

    def session_candles(self, contract: FuturesContract, day: date) -> list[Candle]:
        """The day's closed candles so far."""
        start = datetime.combine(day, time(9, 0), tzinfo=IST)
        end = datetime.combine(day, time(15, 45), tzinfo=IST)
        return self.fetch_candles(contract, start, end)

    def previous_session(
        self, contract: FuturesContract, today: date, lookback_days: int = 10
    ) -> tuple[date, list[Candle]]:
        """Candles of the most recent session before `today` (skips weekends and holidays)."""
        start = datetime.combine(today - timedelta(days=lookback_days), time(9, 0), tzinfo=IST)
        end = datetime.combine(today, time(0, 0), tzinfo=IST)
        by_day: dict[date, list[Candle]] = defaultdict(list)
        for candle in self.fetch_candles(contract, start, end):
            by_day[candle.start.date()].append(candle)
        if not by_day:
            raise NoSessionDataError(
                f"No session candles in the {lookback_days} days before {today}"
            )
        day = max(by_day)
        return day, by_day[day]

    # -- expiries ------------------------------------------------------------------------
    def option_expiries(self, today: date | None = None) -> list[date]:
        today = today or self._clock.now().date()

        def fetch(year: int, month: int) -> Any:
            return self._client.get_expiries(
                exchange=self._inst.exchange,
                underlying_symbol=self._inst.underlying,
                year=year,
                month=month,
            )

        return collect_expiries(fetch, today)

    def nearest_option_expiry(self, today: date | None = None) -> date:
        today = today or self._clock.now().date()
        return choose_option_expiry(self.option_expiries(today), today)

    # -- option chain and contracts -------------------------------------------------------
    def option_chain(self, expiry: date) -> OptionChain:
        """Live chain with greeks for one expiry (priced now, so call it at signal time)."""
        res = self._client.get_option_chain(
            exchange=self._inst.exchange,
            underlying=self._inst.underlying,
            expiry_date=expiry.isoformat(),
        )
        return parse_option_chain(res, expiry)

    def option_contracts(self, expiry: date) -> dict[str, OptionContract]:
        """Lot size, tick size and symbols of one expiry, from the instruments file (cached)."""
        if expiry not in self._contracts:
            self._contracts[expiry] = parse_option_contracts(
                self._underlying_rows(), expiry, self._inst.underlying
            )
        return self._contracts[expiry]

    # -- prices --------------------------------------------------------------------------
    def ltp(self, trading_symbols: Sequence[str]) -> dict[str, float]:
        """Last traded prices keyed by trading symbol (no exchange prefix)."""
        prefix = f"{self._inst.exchange}_"
        keys = ",".join(prefix + s for s in trading_symbols)
        res = self._client.get_ltp(exchange_trading_symbols=keys, segment=self._inst.segment)
        return {k.removeprefix(prefix): float(v) for k, v in res.items() if v is not None}
