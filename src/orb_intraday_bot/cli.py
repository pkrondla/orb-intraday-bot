"""Command line: `orb-bot <command>` (or `python -m orb_intraday_bot <command>`).

    run               the live paper session for today (start it before 09:15 IST)
    replay DATE...    replay past days on real futures candles (synthetic option prices)
    sweep             compare config variants on the same candles
    report            forward-test statistics from the database
    reconcile [DATE]  compare a live day with a replay of the same candles
    status            is a run alive, and what is it doing
    summary [DATE]    trades and P&L of a day from the database
    check             test the connection, contract, pivots, option chain and alerts

Paper only. No command places an order.

This module only parses arguments, calls a service in `orb_intraday_bot.app`, and prints. The
logic lives in the services so it can be tested without a terminal.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from datetime import date, datetime

from orb_intraday_bot import __version__
from orb_intraday_bot.app.context import AppContext, build_market_data, load_context
from orb_intraday_bot.app.forward import (
    NothingToDo,
    day_summary_text,
    forward_report,
    reconcile_day,
)
from orb_intraday_bot.app.health import CheckStep, run_checks, status_report
from orb_intraday_bot.app.replay import (
    PreparedReplay,
    ReplayRequest,
    format_replay_report,
    prepare_replay,
    write_trades_csv,
)
from orb_intraday_bot.core.clock import IST, SystemClock
from orb_intraday_bot.runtime.day import CandleReport
from orb_intraday_bot.storage.store import TradeStore


def _no_database(ctx: AppContext) -> int:
    print(f"No database yet at {ctx.database_path}.")
    return 1


def _today() -> date:
    return datetime.now(IST).date()


# -- run -----------------------------------------------------------------------------------
def cmd_run(_: argparse.Namespace) -> int:
    from orb_intraday_bot.analysis.levels import levels_path, remember
    from orb_intraday_bot.indicators.pivots import SessionLevels
    from orb_intraday_bot.runtime.live import LiveRunner
    from orb_intraday_bot.runtime.logs import new_run_id
    from orb_intraday_bot.runtime.notify import build_notifier
    from orb_intraday_bot.runtime.output import make_output

    ctx = load_context()
    clock = SystemClock()
    store = TradeStore(ctx.database_path)
    data = build_market_data(ctx)

    def save_levels(symbol: str, levels: SessionLevels) -> None:
        # The exchange close is only available in the morning; keep it for replay and reconcile.
        remember(levels_path(ctx.history_path(symbol)), levels)

    runner = LiveRunner(
        ctx.cfg, ctx.data_dir, data, store, build_notifier(ctx.settings),
        clock, time.sleep, make_output(ctx.data_dir, clock.now().date(), new_run_id(clock.now())),
        on_levels=save_levels,
    )
    outcome = runner.run()
    store.close()
    if outcome.exit_code not in (0, 130):
        print(f"Run ended: {outcome.message}", file=sys.stderr)
    return outcome.exit_code


# -- replay and sweep ----------------------------------------------------------------------
def _request(args: argparse.Namespace) -> ReplayRequest:
    return ReplayRequest(
        dates=tuple(getattr(args, "dates", ()) or ()),
        first=getattr(args, "first", None),
        to=getattr(args, "to", None),
        last=getattr(args, "last", None),
        refresh=bool(getattr(args, "refresh", False)),
        min_candles=int(getattr(args, "min_candles", 0) or 0),
        overrides=tuple(getattr(args, "set", ()) or ()),
    )


def _prepare_with(ctx: AppContext, request: ReplayRequest) -> PreparedReplay:
    return prepare_replay(
        ctx.cfg, ctx.history_path, build_market_data(ctx), request, _today()
    )


def cmd_replay(args: argparse.Namespace) -> int:
    from orb_intraday_bot.analysis.backtest import run_range
    from orb_intraday_bot.analysis.stats import rows_from_results
    from orb_intraday_bot.runtime.report import (
        format_candle_report,
        format_exit,
        format_outcome,
    )

    ctx = load_context()
    request = _request(args)
    prep = _prepare_with(ctx, request)
    if args.set:
        print(f"config overrides: {', '.join(args.set)}")

    def on_day(day: date) -> None:
        if args.candles or args.events:
            print(f"\n=== {day} {prep.contract.trading_symbol}")

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
    results = run_range(
        prep.cfg, prep.history.candles, prep.contract.trading_symbol, prep.contract.lot_size,
        prep.first, prep.last, only=prep.only, today=prep.today,
        min_candles=request.min_candles, on_day=on_day, on_report=detail, levels=prep.levels,
    )
    if not results:
        print("No sessions to replay in that range (or no earlier session for the pivots).")
        return 1
    print(format_replay_report(results, prep.cfg, show_trades=args.trades))
    if args.csv:
        write_trades_csv(rows_from_results(results), args.csv)
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

    ctx = load_context()
    prep = _prepare_with(ctx, _request(args))
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
        levels=prep.levels,
    )
    days = len(results[0].results)
    print(f"\n{days} sessions, {prep.contract.trading_symbol}. Replay prices are synthetic; read "
          "the points columns.\n")
    print(format_sweep(results))
    return 0


# -- forward test: report, reconcile, summary ----------------------------------------------
def cmd_report(args: argparse.Namespace) -> int:
    ctx = load_context()
    if not ctx.database_path.exists():
        return _no_database(ctx)
    store = TradeStore(ctx.database_path)
    try:
        report = forward_report(
            store,
            date.fromisoformat(args.first) if args.first else None,
            date.fromisoformat(args.to) if args.to else None,
        )
    finally:
        store.close()
    print(report.text())
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    ctx = load_context()
    today = _today()
    day = date.fromisoformat(args.date) if args.date else today
    if not ctx.database_path.exists():
        return _no_database(ctx)
    try:
        text = reconcile_day(
            ctx.cfg, ctx.database_path, ctx.history_path, build_market_data(ctx), day, today
        )
    except NothingToDo as stop:
        print(stop)
        return 1
    print(text)
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    ctx = load_context()
    if not ctx.database_path.exists():
        return _no_database(ctx)
    day = date.fromisoformat(args.date) if args.date else _today()
    store = TradeStore(ctx.database_path)
    try:
        print(day_summary_text(store, day))
    finally:
        store.close()
    return 0


# -- status and check ----------------------------------------------------------------------
def cmd_status(_: argparse.Namespace) -> int:
    ctx = load_context()
    report = status_report(ctx.data_dir / ctx.cfg.runtime.heartbeat_file, datetime.now(IST))
    print(report.text)
    return report.exit_code


def cmd_serve(_: argparse.Namespace) -> int:
    """Always-on service: sleep until the next session, run it, repeat."""
    from orb_intraday_bot.app.serve import main as serve_main

    return serve_main()


def cmd_watchdog(_: argparse.Namespace) -> int:
    """Independent dead-man monitor (alert only; never trades)."""
    from orb_intraday_bot.app.watchdog import main as watchdog_main

    return watchdog_main()


def cmd_flatten(_: argparse.Namespace) -> int:
    """Request a flatten: write data/FLATTEN for the running session to pick up."""
    ctx = load_context()
    marker = ctx.data_dir / "FLATTEN"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("cli\n", encoding="utf-8")
    print(f"Flatten requested ({marker}). The running session will close open positions.")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    from orb_intraday_bot.runtime.notify import NullNotifier, build_notifier

    ctx = load_context()
    print("ORB Intraday Bot - connection check (read-only)")
    print("  OK    paper mode: LIVE_TRADING is false, no order methods reachable")

    def show(step: CheckStep) -> None:
        print(step.line())

    steps = run_checks(
        build_market_data(ctx), ctx.cfg, ctx.database_path, _today(), on_step=show
    )
    if steps and not steps[0].ok:
        return 1
    failures = sum(1 for s in steps if not s.ok)
    notifier = build_notifier(ctx.settings)
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
    sub.add_parser(
        "serve", help="always-on service: run each trading day, sleep between sessions"
    ).set_defaults(func=cmd_serve)
    sub.add_parser(
        "watchdog", help="dead-man monitor: Telegram alert if the bot goes silent"
    ).set_defaults(func=cmd_watchdog)
    sub.add_parser(
        "flatten", help="request flatten of open positions (writes data/FLATTEN)"
    ).set_defaults(func=cmd_flatten)
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
