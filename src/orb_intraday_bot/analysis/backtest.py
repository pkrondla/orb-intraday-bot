"""Replay many days of real futures candles through the same code the live bot runs.

Option prices are synthetic (a flat premium and delta, see `runtime/synthetic.py`), so the
futures points are the meaningful result; rupee figures only show the mechanics and the charges.
Each day gets a fresh in-memory database and the previous session's pivots, as a live morning
would. The pivots come from saved `SessionLevels` (daily-bar high and low and the exchange close,
as TradingView has them) when a day has them, and from the previous session's 3-minute candles
otherwise.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta

from orb_intraday_bot.config.settings import StrategyConfig
from orb_intraday_bot.core.models import Candle
from orb_intraday_bot.indicators.pivots import SessionLevels, pivots_from_session
from orb_intraday_bot.options.planner import OptionPlanner
from orb_intraday_bot.paper.models import Position
from orb_intraday_bot.runtime.day import CandleReport, TradingDay
from orb_intraday_bot.runtime.synthetic import SyntheticOptions
from orb_intraday_bot.storage.store import TradeStore

FULL_SESSION_CANDLES = 125         # 09:15 to 15:30 in 3-minute candles


@dataclass(frozen=True, slots=True)
class SignalRecord:
    signal_id: str
    time: str                      # HH:MM of the signal candle
    setup: str
    level: str
    direction: str
    status: str                    # accepted | rejected | released
    stage: str
    reason: str


@dataclass(slots=True)
class DayResult:
    day: date
    candle_count: int
    positions: list[Position] = field(default_factory=list)
    signals: list[SignalRecord] = field(default_factory=list)
    previous_day: date | None = None


def split_sessions(candles: Iterable[Candle]) -> dict[date, list[Candle]]:
    by_day: dict[date, list[Candle]] = defaultdict(list)
    for candle in candles:
        by_day[candle.start.date()].append(candle)
    return {d: sorted(c, key=lambda x: x.start) for d, c in sorted(by_day.items())}


def missing_candles(candles: list[Candle]) -> int:
    """How many 3-minute candles are absent between the first and last of a session."""
    if len(candles) < 2:
        return 0
    span = (candles[-1].start - candles[0].start) // timedelta(minutes=3)
    return int(span + 1 - len(candles))


def signal_records(store: TradeStore, day: date) -> list[SignalRecord]:
    return [
        SignalRecord(
            signal_id=row["signal_id"],
            time=str(row["candle_start"])[11:16],
            setup=row["setup"],
            level=row["level_name"],
            direction=row["direction"],
            status=row["status"],
            stage=row["stage"] or "",
            reason=row["reason"] or "",
        )
        for row in store.signals_for_day(day)
    ]


def run_day(
    cfg: StrategyConfig,
    day: date,
    symbol: str,
    lot_size: int,
    previous: list[Candle],
    candles: list[Candle],
    previous_day: date | None = None,
    on_report: Callable[[CandleReport], None] | None = None,
    force_close: bool = True,
    levels: SessionLevels | None = None,
) -> DayResult:
    """`levels` are the previous session's exchange-style high, low and close; without them the
    pivots come from the previous session's own 3-minute candles."""
    store = TradeStore(":memory:")
    pivots = levels.pivots() if levels is not None else pivots_from_session(previous)
    trading = TradingDay(
        cfg, day, symbol, pivots,
        OptionPlanner(cfg, SyntheticOptions(lot_size)), store,
    )
    for candle in candles:
        report = trading.on_candle(candle)
        if on_report is not None:
            on_report(report)
    if force_close and candles and trading.paper.open_positions:
        # The 15:27 candle is missing: close at the last price, as the live runner does.
        last = candles[-1]
        trading.paper.close_all(last.start + timedelta(minutes=3), last.close)
    result = DayResult(
        day=day,
        candle_count=len(candles),
        positions=trading.positions_today(),
        signals=signal_records(store, day),
        previous_day=previous_day,
    )
    store.close()
    return result


def run_range(
    cfg: StrategyConfig,
    candles: Iterable[Candle],
    symbol: str,
    lot_size: int,
    first: date | None = None,
    last: date | None = None,
    only: set[date] | None = None,
    today: date | None = None,
    min_candles: int = 0,
    on_day: Callable[[date], None] | None = None,
    on_report: Callable[[CandleReport], None] | None = None,
    levels: Mapping[date, SessionLevels] | None = None,
) -> list[DayResult]:
    """Every session in `candles` that has an earlier session in the data to take pivots from.

    `levels` maps a session to its saved high, low and close; a day with no entry takes its
    pivots from the previous session's candles."""
    sessions = split_sessions(candles)
    days = list(sessions)
    results = []
    for index, day in enumerate(days):
        if index == 0:
            continue                    # no previous session in the data: no pivots
        if (first and day < first) or (last and day > last):
            continue
        if only is not None and day not in only:
            continue
        if len(sessions[day]) < min_candles:
            continue
        prev_day = days[index - 1]
        if on_day is not None:
            on_day(day)
        results.append(
            run_day(
                cfg, day, symbol, lot_size, sessions[prev_day], sessions[day], prev_day, on_report,
                force_close=today is None or day < today,
                levels=levels.get(prev_day) if levels is not None else None,
            )
        )
    return results
