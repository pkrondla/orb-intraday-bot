"""Groww implementation of market data access (read-only)."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

from orb_intraday_bot.config.settings import InstrumentConfig
from orb_intraday_bot.core.clock import IST, Clock, SystemClock
from orb_intraday_bot.core.errors import DataUnavailableError
from orb_intraday_bot.core.models import Candle
from orb_intraday_bot.data.candles import parse_candle_rows, parse_timestamp
from orb_intraday_bot.data.contract import FuturesContract, select_futures_contract
from orb_intraday_bot.data.expiries import choose_option_expiry, collect_expiries
from orb_intraday_bot.indicators.pivots import (
    CLOSE_CANDLE,
    CLOSE_EXCHANGE,
    CLOSE_VWAP,
    SessionLevels,
    levels_from_candles,
)
from orb_intraday_bot.options.chain import (
    OptionChain,
    OptionContract,
    parse_option_chain,
    parse_option_contracts,
)

_FMT = "%Y-%m-%d %H:%M:%S"
_MAX_DAYS_PER_REQUEST = 28   # Groww allows 30 days of 3-minute candles per request
_MAX_DAYS_DAILY = 180        # daily bars: a long span is accepted in one request
_CLOSING_FROM = time(15, 0)  # closing window used for the closing-price VWAP
_CLOSING_TO = time(15, 40)
_SESSION_END = time(15, 30)

log = logging.getLogger(__name__)


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

    # -- the exchange's own daily numbers (what TradingView's daily bar shows) ------------
    def daily_ranges(
        self, contract: FuturesContract, first: date, last: date
    ) -> dict[date, tuple[float, float]]:
        """High and low of each daily bar. The daily bar includes the pre-open trade."""
        out: dict[date, tuple[float, float]] = {}
        cursor = first
        while cursor <= last:
            chunk_last = min(cursor + timedelta(days=_MAX_DAYS_DAILY), last)
            res = self._client.get_historical_candles(
                exchange=self._inst.exchange,
                segment=self._inst.segment,
                groww_symbol=contract.groww_symbol,
                start_time=f"{cursor.isoformat()} 00:00:00",
                end_time=f"{(chunk_last + timedelta(days=1)).isoformat()} 00:00:00",
                candle_interval=self._client.CANDLE_INTERVAL_DAY,
            )
            for row in res.get("candles") or []:
                if len(row) < 4 or row[2] is None or row[3] is None:
                    continue
                out[parse_timestamp(str(row[0])).date()] = (float(row[2]), float(row[3]))
            cursor = chunk_last + timedelta(days=1)
        return out

    def exchange_previous_close(self, contract: FuturesContract) -> float | None:
        """The exchange's close of the previous session, from the live quote.

        While a session is open the quote's `ohlc.close` is the previous session's official
        close. It is cross-checked against `last_price - day_change`, which must agree.
        """
        quote = self._client.get_quote(
            exchange=self._inst.exchange,
            segment=self._inst.segment,
            trading_symbol=contract.trading_symbol,
        )
        raw = (quote.get("ohlc") or {}).get("close")
        if raw is None or float(raw) <= 0:
            return None
        close = float(raw)
        last, change = quote.get("last_price"), quote.get("day_change")
        disagrees = (
            last is not None
            and change is not None
            and abs(float(last) - float(change) - close) > 0.5
        )
        return None if disagrees else close

    def closing_vwap(self, contract: FuturesContract, day: date) -> float | None:
        """Volume-weighted average of the 1-minute closes in the closing window (15:00 to 15:40).

        Matches the exchange's closing price to within a few paise on the day it was checked
        against (15 Oct 2026). Used for past days, where the quote no longer has the number.
        """
        res = self._client.get_historical_candles(
            exchange=self._inst.exchange,
            segment=self._inst.segment,
            groww_symbol=contract.groww_symbol,
            start_time=f"{day.isoformat()} {_CLOSING_FROM:%H:%M:%S}",
            end_time=f"{day.isoformat()} 15:45:00",
            candle_interval=self._client.CANDLE_INTERVAL_MIN_1,
        )
        rows = [r for r in res.get("candles") or [] if len(r) > 5 and r[4] is not None]
        candles = parse_candle_rows(rows, session_only=False)
        window = [
            c for c in candles
            if c.start.date() == day and _CLOSING_FROM <= c.start.time() < _CLOSING_TO
        ]
        volume = sum(c.volume for c in window)
        if not window or volume <= 0:
            return None
        return sum(c.close * c.volume for c in window) / volume

    def previous_session_levels(
        self, contract: FuturesContract, today: date, lookback_days: int = 10
    ) -> SessionLevels:
        """The previous session's high, low and close for the pivots, as TradingView has them.

        High and low: the daily bar (includes the pre-open trade). Close, best first: the
        exchange's reported close (only while `today`'s session is open), the closing VWAP,
        then the last 3-minute candle. Every optional lookup that fails falls back quietly.
        """
        day, candles = self.previous_session(contract, today, lookback_days)
        base = levels_from_candles(day, candles)
        high, low = base.high, base.low
        try:
            daily = self.daily_ranges(contract, day, day).get(day)
        except Exception as exc:
            log.warning("daily bar unavailable, using candle high/low: %s", exc)
            daily = None
        if daily is not None and daily[0] >= high and daily[1] <= low:
            high, low = daily

        close, source = base.close, CLOSE_CANDLE
        now = self._clock.now()
        if today == now.date() and now.time() < _SESSION_END:
            try:
                official = self.exchange_previous_close(contract)
            except Exception as exc:
                log.warning("exchange close unavailable: %s", exc)
                official = None
            if official is not None and low - 1 <= official <= high + 1:
                return SessionLevels(day, high, low, official, CLOSE_EXCHANGE)
        try:
            vwap = self.closing_vwap(contract, day)
        except Exception as exc:
            log.warning("closing VWAP unavailable, using the last candle: %s", exc)
            vwap = None
        if vwap is not None and low - 1 <= vwap <= high + 1:
            close, source = vwap, CLOSE_VWAP
        return SessionLevels(day, high, low, close, source)

    def historical_levels(
        self, contract: FuturesContract, first: date, last: date
    ) -> dict[date, SessionLevels]:
        """Levels for every finished session in a range (for replay). A day whose closing VWAP
        cannot be built is left out, and replay falls back to its candles for that day."""
        today = self._clock.now().date()
        out: dict[date, SessionLevels] = {}
        for day, (high, low) in sorted(self.daily_ranges(contract, first, last).items()):
            if day >= today:
                continue
            try:
                vwap = self.closing_vwap(contract, day)
            except Exception as exc:
                log.warning("closing VWAP for %s unavailable: %s", day, exc)
                continue
            if vwap is not None and low - 1 <= vwap <= high + 1:
                out[day] = SessionLevels(day, high, low, vwap, CLOSE_VWAP)
        return out

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
