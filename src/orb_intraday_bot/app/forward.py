"""Forward-test services: the statistics report, a day summary, and live-versus-replay."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from orb_intraday_bot.analysis.backtest import SignalRecord, run_day, signal_records, split_sessions
from orb_intraday_bot.analysis.history import load_history
from orb_intraday_bot.analysis.levels import levels_path, load_range
from orb_intraday_bot.analysis.reconcile import format_reconciliation, reconcile
from orb_intraday_bot.analysis.stats import TradeRow, format_report, trade_rows
from orb_intraday_bot.app.replay import ReplayData
from orb_intraday_bot.config.settings import StrategyConfig
from orb_intraday_bot.runtime.report import format_day_end
from orb_intraday_bot.storage.store import TradeStore


class NothingToDo(Exception):
    """A command cannot proceed; the message is shown to the user as is (exit code 1)."""


@dataclass(frozen=True)
class ForwardReport:
    days: list[date]
    rows: list[TradeRow]
    signals: list[SignalRecord]
    still_open: int

    def text(self) -> str:
        head = f"Forward test: {len(self.days)} day(s) in the database"
        if self.days:
            head += f" ({self.days[0]} to {self.days[-1]})"
        parts = [head, format_report(self.rows, len(self.days), self.signals)]
        if self.still_open:
            parts.append(f"\n{self.still_open} position(s) still open (not counted).")
        return "\n".join(parts)


def forward_report(
    store: TradeStore, first: date | None = None, last: date | None = None
) -> ForwardReport:
    lo, hi = first or date.min, last or date.max
    days = [d for d in store.trade_days() if lo <= d <= hi]
    rows: list[TradeRow] = []
    signals: list[SignalRecord] = []
    for day in days:
        rows += trade_rows(day, store.positions_for_day(day))
        signals += signal_records(store, day)
    return ForwardReport(days, rows, signals, len(store.open_positions()))


def day_summary_text(store: TradeStore, day: date) -> str:
    lines = [format_day_end(store.day_summary(day), store.positions_for_day(day))]
    for row in store.signals_for_day(day):
        if row["status"] != "accepted":
            lines.append(
                f"  signal {row['candle_start'][11:16]} {row['setup']} {row['level_name']} "
                f"{row['direction']}: {row['status']} [{row['stage']}] {row['reason']}"
            )
    return "\n".join(lines)


def reconcile_day(
    cfg: StrategyConfig,
    database: Path,
    history_path_for: Callable[[str], Path],
    data: ReplayData,
    day: date,
    today: date,
) -> str:
    """Compare a live day in the database with a replay of the same candles."""
    if not database.exists():
        raise NothingToDo(f"No database yet at {database}.")
    contract = data.futures_contract(day)
    history = load_history(
        history_path_for(contract.trading_symbol),
        lambda start, end: data.fetch_candles(contract, start, end),
        day, day, today, refresh=True,
    )
    sessions = split_sessions(history.candles)
    if day not in sessions:
        raise NothingToDo(f"No candles for {day} (holiday, or the session has not started).")
    earlier = [d for d in sessions if d < day]
    if not earlier:
        raise NothingToDo("No earlier session in the data to take pivots from.")
    store = TradeStore(database)
    live_signals = signal_records(store, day)
    live_positions = store.positions_for_day(day)
    store.close()
    if not live_signals and not live_positions:
        raise NothingToDo(
            f"The database has no signals or trades for {day}: the live run did not record "
            "that day (or never ran), so there is nothing to compare."
        )
    prev_day = earlier[-1]
    # The levels the live run saved that morning (the exchange close), so both sides use the
    # same pivots; a day it never saw falls back to the closing VWAP, then to the candles.
    levels = load_range(
        levels_path(history_path_for(contract.trading_symbol)),
        lambda a, b: data.historical_levels(contract, a, b),
        prev_day, prev_day, today,
    )
    replay = run_day(
        cfg, day, contract.trading_symbol, contract.lot_size, sessions[prev_day],
        sessions[day], prev_day, levels=levels.get(prev_day),
    )
    return (
        format_reconciliation(reconcile(day, live_signals, live_positions, replay))
        + f"\n(replay used {replay.candle_count} candles; a full session has 125)"
    )
