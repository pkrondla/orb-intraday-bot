"""Command line: `orb-bot <command>` (or `python -m orb_intraday_bot <command>`).

    run               the live paper session for today (start it before 09:15 IST)
    replay DATE...    replay past days on real futures candles (synthetic option prices)
    status            is a run alive, and what is it doing
    summary [DATE]    trades and P&L of a day from the database
    check             test the connection, contract, pivots, option chain and alerts

Paper only. No command places an order.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from orb_intraday_bot import __version__
from orb_intraday_bot.adapters.groww.market_data import GrowwMarketData
from orb_intraday_bot.analysis.history import History
from orb_intraday_bot.config import Settings
from orb_intraday_bot.config.settings import (
    _PROJECT_ROOT,
    StrategyConfig,
    apply_overrides,
    load_strategy_config,
)
from orb_intraday_bot.core.clock import IST, SystemClock
from orb_intraday_bot.data.contract import FuturesContract
from orb_intraday_bot.runtime.day import CandleReport
from orb_intraday_bot.runtime.guard import run_startup_checks


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else _PROJECT_ROOT / path


def _load() -> tuple[Settings, StrategyConfig, Path]:
    settings = Settings()
    run_startup_checks(settings)
    settings.data_dir = _resolve(settings.data_dir)
    cfg = load_strategy_config(_resolve(settings.strategy_config_path))
    return settings, cfg, settings.data_dir


def _market_data(settings: Settings, cfg: StrategyConfig) -> GrowwMarketData:
    from orb_intraday_bot.adapters.groww.factory import build_readonly_client

    return GrowwMarketData(build_readonly_client(settings), cfg.instrument)


# -- run -----------------------------------------------------------------------------------
def cmd_run(_: argparse.Namespace) -> int:
    from orb_intraday_bot.runtime.live import LiveRunner
    from orb_intraday_bot.runtime.notify import build_notifier
    from orb_intraday_bot.runtime.output import make_output
    from orb_intraday_bot.storage.store import TradeStore

    settings, cfg, data_dir = _load()
    clock = SystemClock()
    store = TradeStore(data_dir / cfg.paper.database_file)
    runner = LiveRunner(
        cfg, data_dir, _market_data(settings, cfg), store, build_notifier(settings), clock,
        time.sleep, make_output(data_dir, clock.now().date()),
    )
    outcome = runner.run()
    store.close()
    if outcome.exit_code not in (0, 130):
        print(f"Run ended: {outcome.message}", file=sys.stderr)
    return outcome.exit_code


# -- replay --------------------------------------------------------------------------------
def _history_path(data_dir: Path, symbol: str) -> Path:
    return data_dir / "history" / f"{symbol}.json"


def _config_with_overrides(cfg: StrategyConfig, args: argparse.Namespace) -> StrategyConfig:
    overrides = getattr(args, "set", None) or []
    return apply_overrides(cfg, overrides) if overrides else cfg


def _requested_days(args: argparse.Namespace, today: date) -> tuple[date, date, set[date] | None]:
    """The first and last date to fetch, and the exact days wanted (None = every session)."""
    if args.dates:
        days = {date.fromisoformat(d) for d in args.dates}
        return min(days), max(days), days
    if args.first or args.to:
        last = date.fromisoformat(args.to) if args.to else today
        first = date.fromisoformat(args.first) if args.first else last - timedelta(days=60)
        return first, last, None
    if args.last:
        return today - timedelta(days=args.last * 2 + 10), today, None
    return today, today, {today}


@dataclass(frozen=True)
class _Prepared:
    cfg: StrategyConfig
    contract: FuturesContract
    history: History
    today: date
    first: date
    last: date
    only: set[date] | None


def _prepare_replay(args: argparse.Namespace) -> _Prepared:
    """Config (with overrides), the futures contract and the cached candles for the request."""
    from orb_intraday_bot.analysis.backtest import split_sessions
    from orb_intraday_bot.analysis.history import load_history

    settings, base_cfg, data_dir = _load()
    cfg = _config_with_overrides(base_cfg, args)
    md = _market_data(settings, cfg)
    today = datetime.now(IST).date()
    first, last, wanted = _requested_days(args, today)
    contract = md.futures_contract(last)
    history = load_history(
        _history_path(data_dir, contract.trading_symbol),
        lambda start, end: md.fetch_candles(contract, start, end),
        first, last, today, refresh=args.refresh,
    )
    only = wanted
    if args.last and wanted is None and not (args.first or args.to):
        only = set(list(split_sessions(history.candles))[-args.last:])
    return _Prepared(cfg, contract, history, today, first, last, only)


def cmd_replay(args: argparse.Namespace) -> int:
    import csv

    from orb_intraday_bot.analysis.backtest import DayResult, run_range
    from orb_intraday_bot.analysis.stats import (
        check_results,
        data_quality,
        format_report,
        rows_from_results,
    )
    from orb_intraday_bot.paper.report import format_closed_position, format_summary, summarize
    from orb_intraday_bot.runtime.report import (
        format_candle_report,
        format_exit,
        format_outcome,
    )

    prep = _prepare_replay(args)
    cfg, contract, history, today = prep.cfg, prep.contract, prep.history, prep.today
    first, last, only = prep.first, prep.last, prep.only
    if args.set:
        print(f"config overrides: {', '.join(args.set)}")

    def on_day(day: date) -> None:
        if args.candles or args.events:
            print(f"\n=== {day} {contract.trading_symbol}")

    def on_report(report: CandleReport) -> None:
        stamp = f"{report.candle.start:%H:%M}"
        if args.candles:
            print(format_candle_report(report))
            return
        for outcome in report.outcomes:
            if outcome.status in {"opened", "rejected"}:
                print(f"{stamp}  {format_outcome(outcome)}")
        for event in report.exits:
            print(f"{stamp}  {format_exit(event)}")

    detail = on_report if (args.candles or args.events) else None
    results: list[DayResult] = run_range(
        cfg, history.candles, contract.trading_symbol, contract.lot_size, first, last,
        only=only, today=today, min_candles=args.min_candles, on_day=on_day, on_report=detail,
    )
    if not results:
        print("No sessions to replay in that range (or no earlier session for the pivots).")
        return 1
    for res in results:
        gaps = f"  [{res.candle_count} candles]" if res.candle_count != 125 else ""
        print(format_summary(summarize(res.day, res.positions)) + gaps)
        if args.trades:
            for p in res.positions:
                print(f"    {p.opened_at:%H:%M} {format_closed_position(p)}")
    rows = rows_from_results(results)
    print()
    print(format_report(rows, len(results), [s for r in results for s in r.signals]))
    problems = check_results(results, cfg)
    print()
    if problems:
        print(f"{len(problems)} sanity finding(s):")
        for problem in problems:
            print(f"  ! {problem}")
    else:
        print("Sanity checks: all passed.")
    quality = data_quality(results)
    if quality:
        print(quality)
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                ["day", "time", "setup", "level", "direction", "zone", "points", "risk_points",
                 "r", "net_inr", "charges_inr", "exits", "minutes_held"]
            )
            for r in rows:
                writer.writerow(
                    [r.day, f"{r.position.opened_at:%H:%M}", r.setup, r.level, r.direction,
                     r.zone, r.points, round(r.risk_points, 1), round(r.r, 2), r.net,
                     r.charges, r.exits, round(r.minutes_held)]
                )
        print(f"Trades written to {args.csv}")
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    from orb_intraday_bot.analysis.sweep import (
        DEFAULT_VARIANTS,
        Variant,
        format_sweep,
        parse_variant,
        run_sweep,
    )

    prep = _prepare_replay(args)
    variants = [Variant("baseline")]
    if args.variant:
        variants += [parse_variant(v) for v in args.variant]
    else:
        variants += [v for v in DEFAULT_VARIANTS if v.name != "baseline"]
    if args.set:
        print(f"base config overrides: {', '.join(args.set)}")
    results = run_sweep(
        prep.cfg, variants, prep.history.candles, prep.contract.trading_symbol,
        prep.contract.lot_size, prep.first, prep.last, prep.only, prep.today,
        args.min_candles, progress=lambda name: print(f"  running {name} ...", flush=True),
    )
    days = len(results[0].results)
    print(f"\n{days} sessions, {prep.contract.trading_symbol}. Replay prices are synthetic; read "
          "the points columns.\n")
    print(format_sweep(results))
    return 0


# -- report (forward test) and reconcile ---------------------------------------------------
def cmd_report(args: argparse.Namespace) -> int:
    from orb_intraday_bot.analysis.backtest import signal_records
    from orb_intraday_bot.analysis.stats import format_report, trade_rows
    from orb_intraday_bot.storage.store import TradeStore

    _settings, cfg, data_dir = _load()
    path = data_dir / cfg.paper.database_file
    if not path.exists():
        print(f"No database yet at {path}.")
        return 1
    store = TradeStore(path)
    first = date.fromisoformat(args.first) if args.first else date.min
    last = date.fromisoformat(args.to) if args.to else date.max
    days = [d for d in store.trade_days() if first <= d <= last]
    rows = []
    signals = []
    for day in days:
        rows += trade_rows(day, store.positions_for_day(day))
        signals += signal_records(store, day)
    still_open = len(store.open_positions())
    store.close()
    print(f"Forward test: {len(days)} day(s) in the database"
          + (f" ({days[0]} to {days[-1]})" if days else ""))
    print(format_report(rows, len(days), signals))
    if still_open:
        print(f"\n{still_open} position(s) still open (not counted).")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    from orb_intraday_bot.analysis.backtest import run_day, signal_records, split_sessions
    from orb_intraday_bot.analysis.history import load_history
    from orb_intraday_bot.analysis.reconcile import format_reconciliation, reconcile
    from orb_intraday_bot.storage.store import TradeStore

    settings, cfg, data_dir = _load()
    path = data_dir / cfg.paper.database_file
    if not path.exists():
        print(f"No database yet at {path}.")
        return 1
    today = datetime.now(IST).date()
    day = date.fromisoformat(args.date) if args.date else today
    md = _market_data(settings, cfg)
    contract = md.futures_contract(day)
    history = load_history(
        _history_path(data_dir, contract.trading_symbol),
        lambda start, end: md.fetch_candles(contract, start, end),
        day, day, today, refresh=True,
    )
    sessions = split_sessions(history.candles)
    if day not in sessions:
        print(f"No candles for {day} (holiday, or the session has not started).")
        return 1
    earlier = [d for d in sessions if d < day]
    if not earlier:
        print("No earlier session in the data to take pivots from.")
        return 1
    store = TradeStore(path)
    live_signals = signal_records(store, day)
    live_positions = store.positions_for_day(day)
    store.close()
    if not live_signals and not live_positions:
        print(f"The database has no signals or trades for {day}: the live run did not record "
              "that day (or never ran), so there is nothing to compare.")
        return 1
    replay = run_day(
        cfg, day, contract.trading_symbol, contract.lot_size, sessions[earlier[-1]],
        sessions[day], earlier[-1],
    )
    print(format_reconciliation(reconcile(day, live_signals, live_positions, replay)))
    print(f"(replay used {replay.candle_count} candles; a full session has 125)")
    return 0


# -- status / summary ----------------------------------------------------------------------
def cmd_status(_: argparse.Namespace) -> int:
    from orb_intraday_bot.runtime.monitor import ACTIVE_STATUSES, read_heartbeat

    _settings, cfg, data_dir = _load()
    info = read_heartbeat(data_dir / cfg.runtime.heartbeat_file)
    now = datetime.now(IST)
    if info is None:
        print("No heartbeat yet: the bot has not run (or the data folder is different).")
        return 1
    age = info.age_sec(now)
    alive = info.status in ACTIVE_STATUSES and age < 120
    verdict = "ALIVE" if alive else ("STALE - process stuck or dead?" if info.status in
                                     ACTIVE_STATUSES else "not running")
    print(f"status: {info.status} ({verdict})  heartbeat {age:.0f}s ago  pid {info.pid}")
    print(f"last candle: {info.last_candle or '-'}  open positions: {info.open_positions}  "
          f"errors: {info.errors}")
    if info.message:
        print(f"message: {info.message}")
    return 0 if (alive or info.status in {"finished", "stopped"}) else 1


def cmd_summary(args: argparse.Namespace) -> int:
    from orb_intraday_bot.runtime.report import format_day_end
    from orb_intraday_bot.storage.store import TradeStore

    _, cfg, data_dir = _load()
    path = data_dir / cfg.paper.database_file
    if not path.exists():
        print(f"No database yet at {path}.")
        return 1
    day = date.fromisoformat(args.date) if args.date else datetime.now(IST).date()
    store = TradeStore(path)
    print(format_day_end(store.day_summary(day), store.positions_for_day(day)))
    for row in store.signals_for_day(day):
        if row["status"] != "accepted":
            print(f"  signal {row['candle_start'][11:16]} {row['setup']} {row['level_name']} "
                  f"{row['direction']}: {row['status']} [{row['stage']}] {row['reason']}")
    store.close()
    return 0


# -- check ---------------------------------------------------------------------------------
def cmd_check(args: argparse.Namespace) -> int:
    from orb_intraday_bot.core.models import Direction
    from orb_intraday_bot.indicators.pivots import pivots_from_session
    from orb_intraday_bot.options.selector import kind_for, select_option
    from orb_intraday_bot.runtime.notify import NullNotifier, build_notifier
    from orb_intraday_bot.storage.store import TradeStore

    failures = 0

    def step(name: str, fn: Callable[[], str]) -> bool:
        nonlocal failures
        try:
            detail = fn()
            print(f"  OK    {name}: {detail}")
            return True
        except Exception as exc:
            failures += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
            return False

    settings, cfg, data_dir = _load()
    print("ORB Intraday Bot - connection check (read-only)")
    print("  OK    paper mode: LIVE_TRADING is false, no order methods reachable")
    md = _market_data(settings, cfg)
    today = datetime.now(IST).date()
    contracts: list[FuturesContract] = []

    def connect() -> str:
        found = md.futures_contract(today)
        contracts.append(found)
        return f"{found.trading_symbol}, expiry {found.expiry}, lot {found.lot_size}"

    if not step("login and contract", connect):
        return 1
    contract = contracts[0]

    def pivots() -> str:
        day, prev = md.previous_session(contract, today)
        p = pivots_from_session(prev)
        return f"{day}: {len(prev)} candles, S1 {p.s1:.1f} P {p.p:.1f} R1 {p.r1:.1f}"

    step("previous session and pivots", pivots)
    step("nearest weekly expiry", lambda: str(md.nearest_option_expiry(today)))

    def chain() -> str:
        expiry_date = md.nearest_option_expiry(today)
        price = md.ltp([contract.trading_symbol])[contract.trading_symbol]
        chain_data = md.option_chain(expiry_date)
        parts = []
        for direction in (Direction.LONG, Direction.SHORT):
            q = select_option(direction, price, chain_data, cfg.options).quote
            parts.append(
                f"{kind_for(direction).value} {q.trading_symbol} delta {q.delta} ltp {q.ltp}"
            )
        return f"futures {price}; " + "; ".join(parts)

    step("option chain and strike pick", chain)

    def database() -> str:
        path = data_dir / cfg.paper.database_file
        TradeStore(path).close()
        return str(path)

    step("database", database)

    notifier = build_notifier(settings)
    if isinstance(notifier, NullNotifier):
        print("  --    telegram: not configured (optional)")
    else:
        print("  OK    telegram: configured")
        if args.send_test_alert:
            notifier.send("ORB bot: test alert. Telegram is working.")
            print("        test alert sent")
    print("All checks passed." if failures == 0 else f"{failures} check(s) failed.")
    return 0 if failures == 0 else 1


# -- parser --------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orb-bot", description="ORB Intraday Bot (paper trading only, never places orders)"
    )
    parser.add_argument("--version", action="version", version=f"orb-bot {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="live paper session for today").set_defaults(func=cmd_run)
    replay = sub.add_parser("replay", help="replay past days (synthetic option prices)")
    replay.add_argument("dates", nargs="*", help="YYYY-MM-DD (default: today)")
    replay.add_argument("--from", dest="first", help="first day of a range, YYYY-MM-DD")
    replay.add_argument("--to", help="last day of a range (default: today)")
    replay.add_argument("--last", type=int, metavar="N", help="the most recent N sessions")
    replay.add_argument(
        "--events", "--quiet", dest="events", action="store_true",
        help="print signals, blocked signals and exits as they happen",
    )
    replay.add_argument("--candles", action="store_true", help="print every candle report")
    replay.add_argument("--trades", action="store_true", help="list each day's trades")
    replay.add_argument(
        "--set", action="append", default=[], metavar="KEY=VALUE",
        help="override a config value for this run, e.g. --set targets.t1_max_distance_points=75",
    )
    replay.add_argument(
        "--min-candles", type=int, default=0, help="skip sessions with fewer candles"
    )
    replay.add_argument("--csv", metavar="FILE", help="write every trade to a CSV file")
    replay.add_argument("--refresh", action="store_true", help="refetch candles from Groww")
    replay.set_defaults(func=cmd_replay)
    sweep = sub.add_parser("sweep", help="compare config variants on the same candles")
    sweep.add_argument("dates", nargs="*", help="specific days (default: --last or a range)")
    sweep.add_argument("--from", dest="first", help="first day, YYYY-MM-DD")
    sweep.add_argument("--to", help="last day, YYYY-MM-DD")
    sweep.add_argument("--last", type=int, metavar="N", help="the most recent N sessions")
    sweep.add_argument(
        "--variant", action="append", default=[], metavar="NAME|KEY=VALUE,KEY=VALUE",
        help="a variant to try (repeatable); without any, the built-in grid runs",
    )
    sweep.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    sweep.add_argument("--min-candles", type=int, default=0)
    sweep.add_argument("--refresh", action="store_true")
    sweep.set_defaults(func=cmd_sweep)
    report = sub.add_parser("report", help="forward-test statistics from the database")
    report.add_argument("--from", dest="first", help="first day, YYYY-MM-DD")
    report.add_argument("--to", help="last day, YYYY-MM-DD")
    report.set_defaults(func=cmd_report)
    reconcile = sub.add_parser(
        "reconcile", help="compare a live day with a replay of the same candles"
    )
    reconcile.add_argument("date", nargs="?", help="YYYY-MM-DD (default: today)")
    reconcile.set_defaults(func=cmd_reconcile)
    sub.add_parser("status", help="is a run alive").set_defaults(func=cmd_status)
    summary = sub.add_parser("summary", help="trades and P&L of a day")
    summary.add_argument("date", nargs="?", help="YYYY-MM-DD (default: today)")
    summary.set_defaults(func=cmd_summary)
    check = sub.add_parser("check", help="test connection, contract, option chain, alerts")
    check.add_argument("--send-test-alert", action="store_true", help="send a Telegram test")
    check.set_defaults(func=cmd_check)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
