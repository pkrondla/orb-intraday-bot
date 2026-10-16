"""Shared building blocks for tests that run a trading day or the live runner.

The day used is the real 15 Oct 2026 morning (an ORB long confirms at 09:33). The live runner is
driven by `FakeSource` and a fixed clock whose `sleep` advances time, so a whole session runs in
milliseconds.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

from orb_intraday_bot.config.settings import StrategyConfig
from orb_intraday_bot.core.clock import FixedClock
from orb_intraday_bot.core.models import Candle
from orb_intraday_bot.data.contract import FuturesContract
from orb_intraday_bot.indicators.pivots import (
    SessionLevels,
    levels_from_candles,
    pivots_from_session,
)
from orb_intraday_bot.options.chain import OptionChain
from orb_intraday_bot.options.planner import OptionPlanner
from orb_intraday_bot.runtime.day import TradingDay
from orb_intraday_bot.runtime.live import LiveRunner
from orb_intraday_bot.runtime.synthetic import SyntheticOptions
from orb_intraday_bot.storage.store import TradeStore
from tests.helpers import at, series
from tests.unit.test_retest_and_swing import real_day

__all__ = [
    "CFG", "CONTRACT", "DAY", "LOT", "PREV_DAY", "RUN_CFG", "FakeSource", "Harness", "NoChain",
    "Recorder", "at", "make_day", "prev_session", "real_day",
]

DAY = date(2026, 10, 15)
PREV_DAY = date(2026, 10, 14)
LOT = 75
CFG = StrategyConfig()
# Coarser retry steps keep the simulated five-hour wait after the data ends fast.
RUN_CFG = StrategyConfig.model_validate(
    {"runtime": {"fetch_retry_sec": 60, "poll_interval_sec": 30}}
)
CONTRACT = FuturesContract("NIFTY26OCTFUT", "NIFTY-26Oct", "1", date(2026, 10, 27), LOT, 0.05)


def prev_session() -> list[Candle]:
    return series(
        [(22750, 22900, 22600, 22750), (22750, 22800, 22650, 22750)],
        first=at(9, 15, PREV_DAY),
    )


def make_day(store: TradeStore | None = None, planner: OptionPlanner | None = None) -> TradingDay:
    return TradingDay(
        CFG, DAY, CONTRACT.trading_symbol, pivots_from_session(prev_session()),
        planner or OptionPlanner(CFG, SyntheticOptions(LOT)), store,
    )


class NoChain(SyntheticOptions):
    def option_chain(self, expiry: date) -> OptionChain:
        return OptionChain(expiry, None, {})


class FakeSource(SyntheticOptions):
    """A data source that publishes the day's candles as the fixed clock passes them."""

    def __init__(self, clock: FixedClock, candles: list[Candle]) -> None:
        super().__init__(LOT)
        self.clock = clock
        self.candles = candles
        self.fail_fetches = 0
        self.price_override: tuple[datetime, float] | None = None

    def futures_contract(self, today: date | None = None) -> FuturesContract:
        return CONTRACT

    def previous_session(
        self, contract: FuturesContract, today: date, lookback_days: int = 10
    ) -> tuple[date, list[Candle]]:
        return PREV_DAY, prev_session()

    def previous_session_levels(
        self, contract: FuturesContract, today: date, lookback_days: int = 10
    ) -> SessionLevels:
        return levels_from_candles(PREV_DAY, prev_session())

    def session_candles(self, contract: FuturesContract, day: date) -> list[Candle]:
        if self.fail_fetches > 0:
            self.fail_fetches -= 1
            raise ConnectionError("boom")
        now = self.clock.now()
        return [c for c in self.candles if c.start + timedelta(minutes=3) <= now]

    def ltp(self, trading_symbols: Sequence[str]) -> dict[str, float]:
        price = self.candles[-1].close if self.candles else 22800.0
        if self.price_override and self.clock.now() >= self.price_override[0]:
            price = self.price_override[1]
        return {
            s: (price if s == CONTRACT.trading_symbol else 150.0) for s in trading_symbols
        }


class Recorder:
    """A notifier that remembers what it was asked to send."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> None:
        self.messages.append(text)

    def joined(self) -> str:
        return "\n".join(self.messages)


class Harness:
    """A live runner wired to fakes: clock, source, store, notifier and printed lines."""

    def __init__(
        self, tmp_path: Path, start: datetime, candles: list[Candle] | None = None
    ) -> None:
        self.clock = FixedClock(start)
        self.source = FakeSource(self.clock, real_day() if candles is None else candles)
        self.store = TradeStore(":memory:")
        self.notes = Recorder()
        self.lines: list[str] = []
        self.sleeps = 0
        self.data_dir = tmp_path

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        assert self.sleeps < 100_000, "runner is not advancing"
        self.clock.advance(seconds=seconds)

    def runner(self) -> LiveRunner:
        return LiveRunner(
            RUN_CFG, self.data_dir, self.source, self.store, self.notes, self.clock,
            self.sleep, self.lines.append,
        )

    @property
    def text(self) -> str:
        return "\n".join(self.lines)
