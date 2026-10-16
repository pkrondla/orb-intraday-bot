"""Replay and sweep: choose the days, load the candles, and describe the results."""

from __future__ import annotations

import csv
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from orb_intraday_bot.analysis.backtest import FULL_SESSION_CANDLES, DayResult, split_sessions
from orb_intraday_bot.analysis.history import PIVOT_LOOKBACK_DAYS, History, load_history
from orb_intraday_bot.analysis.levels import levels_path, load_range
from orb_intraday_bot.analysis.stats import (
    TradeRow,
    check_results,
    data_quality,
    format_report,
    rows_from_results,
)
from orb_intraday_bot.config.settings import StrategyConfig, apply_overrides
from orb_intraday_bot.core.models import Candle
from orb_intraday_bot.data.contract import FuturesContract
from orb_intraday_bot.indicators.pivots import SessionLevels
from orb_intraday_bot.paper.report import format_closed_position, format_summary, summarize

CSV_HEADER = [
    "day", "time", "setup", "level", "direction", "zone", "points", "risk_points", "r",
    "net_inr", "charges_inr", "exits", "minutes_held",
]


class ReplayData(Protocol):
    def futures_contract(self, today: date | None = None) -> FuturesContract: ...

    def fetch_candles(
        self, contract: FuturesContract, start: datetime, end: datetime
    ) -> list[Candle]: ...

    def historical_levels(
        self, contract: FuturesContract, first: date, last: date
    ) -> dict[date, SessionLevels]: ...


@dataclass(frozen=True)
class ReplayRequest:
    """Which days to replay, as typed on the command line (dates are ISO strings)."""

    dates: Sequence[str] = ()
    first: str | None = None
    to: str | None = None
    last: int | None = None
    refresh: bool = False
    min_candles: int = 0
    overrides: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class PreparedReplay:
    cfg: StrategyConfig
    contract: FuturesContract
    history: History
    today: date
    first: date
    last: date
    only: set[date] | None
    levels: dict[date, SessionLevels] = field(default_factory=dict)


def requested_days(
    request: ReplayRequest, today: date
) -> tuple[date, date, set[date] | None]:
    """The first and last date to fetch, and the exact days wanted (None = every session)."""
    if request.dates:
        days = {date.fromisoformat(d) for d in request.dates}
        return min(days), max(days), days
    if request.first or request.to:
        last = date.fromisoformat(request.to) if request.to else today
        first = date.fromisoformat(request.first) if request.first else last - timedelta(days=60)
        return first, last, None
    if request.last:
        return today - timedelta(days=request.last * 2 + 10), today, None
    return today, today, {today}


def prepare_replay(
    cfg: StrategyConfig,
    history_path_for: Callable[[str], Path],
    data: ReplayData,
    request: ReplayRequest,
    today: date,
) -> PreparedReplay:
    """Config (with overrides), the futures contract and the cached candles for the request.
    `history_path_for(symbol)` says where that contract's candle cache lives."""
    run_cfg = apply_overrides(cfg, list(request.overrides)) if request.overrides else cfg
    first, last, wanted = requested_days(request, today)
    contract = data.futures_contract(last)
    history = load_history(
        history_path_for(contract.trading_symbol),
        lambda start, end: data.fetch_candles(contract, start, end),
        first, last, today, refresh=request.refresh,
    )
    only = wanted
    if request.last and wanted is None and not (request.first or request.to):
        only = set(list(split_sessions(history.candles))[-request.last:])
    levels = load_range(
        levels_path(history_path_for(contract.trading_symbol)),
        lambda a, b: data.historical_levels(contract, a, b),
        first - timedelta(days=PIVOT_LOOKBACK_DAYS), last, today, refresh=request.refresh,
    )
    return PreparedReplay(run_cfg, contract, history, today, first, last, only, levels)


def format_replay_report(
    results: Sequence[DayResult], cfg: StrategyConfig, show_trades: bool = False
) -> str:
    """Per-day lines, the statistics report, sanity findings and a data-quality note."""
    lines: list[str] = []
    for res in results:
        gaps = (
            f"  [{res.candle_count} candles]" if res.candle_count != FULL_SESSION_CANDLES else ""
        )
        lines.append(format_summary(summarize(res.day, res.positions)) + gaps)
        if show_trades:
            lines += [f"    {p.opened_at:%H:%M} {format_closed_position(p)}" for p in res.positions]
    rows = rows_from_results(results)
    lines += ["", format_report(rows, len(results), [s for r in results for s in r.signals]), ""]
    problems = check_results(results, cfg)
    if problems:
        lines.append(f"{len(problems)} sanity finding(s):")
        lines += [f"  ! {problem}" for problem in problems]
    else:
        lines.append("Sanity checks: all passed.")
    quality = data_quality(results)
    if quality:
        lines.append(quality)
    return "\n".join(lines)


def write_trades_csv(rows: Sequence[TradeRow], path: str | Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_HEADER)
        for r in rows:
            writer.writerow(
                [r.day, f"{r.position.opened_at:%H:%M}", r.setup, r.level, r.direction,
                 r.zone, r.points, round(r.risk_points, 1), round(r.r, 2), r.net,
                 r.charges, r.exits, round(r.minutes_held)]
            )
