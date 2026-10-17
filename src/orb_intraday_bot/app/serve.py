"""Always-on paper (or live) service: sleep until the next session, run it, repeat.

NSSM keeps this process alive. Without the sleep-until-next-day logic, `orb-bot run` would exit
at 15:30 and NSSM would restart it in a tight loop.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path

from orb_intraday_bot import __version__
from orb_intraday_bot.app import operator_text as ot
from orb_intraday_bot.app.context import AppContext, build_market_data, load_context
from orb_intraday_bot.app.forward import NothingToDo, day_summary_text, reconcile_day
from orb_intraday_bot.config.fingerprint import fingerprint
from orb_intraday_bot.core.clock import IST, Clock, SystemClock
from orb_intraday_bot.execution.account import AccountReader
from orb_intraday_bot.indicators.pivots import SessionLevels
from orb_intraday_bot.paper.models import Position
from orb_intraday_bot.risk.engine import RiskEngine
from orb_intraday_bot.risk.limits import RiskLimits, load_risk_limits
from orb_intraday_bot.runtime.alerts import Alerts
from orb_intraday_bot.runtime.arming import (
    configured_mode_label,
    resolve_mode,
)
from orb_intraday_bot.runtime.calendar import load_holidays, next_session_start
from orb_intraday_bot.runtime.executor import LiveExecutor
from orb_intraday_bot.runtime.live import LiveRunner, RunOutcome
from orb_intraday_bot.runtime.logs import log_event, new_run_id
from orb_intraday_bot.runtime.notify import Notifier, build_notifier
from orb_intraday_bot.runtime.output import make_output
from orb_intraday_bot.runtime.telegram_commands import (
    Command,
    CommandBus,
    CommandKind,
    TelegramCommandPoller,
    build_command_poller,
)
from orb_intraday_bot.storage.store import TradeStore

log = logging.getLogger("orb.serve")
STOP_FILE_NAME = "bot.stop"
TRADING_OFF_FILE_NAME = "TRADING_OFF"
SESSION_START = "09:00"
_IDLE_STEP_SEC = 15


@dataclass
class ServeState:
    """Mutable flags the command poller and the runner share."""

    bus: CommandBus
    risk: RiskEngine
    stop_requested: bool = False
    # The running session, for the commands that act on it (/exit, /rearm, /reconcile, ...).
    # None between sessions.
    runner: LiveRunner | None = None
    # The pivot inputs the last session loaded, and the day they are for (for /levels).
    levels: tuple[str, SessionLevels] | None = None
    levels_day: date | None = None


SleepFunc = Callable[[float], None]
OutFunc = Callable[[str], None]
SessionFunc = Callable[[AppContext, Notifier, ServeState, Clock, SleepFunc], RunOutcome]


def stop_file_path(data_dir: Path) -> Path:
    return data_dir / STOP_FILE_NAME


def trading_off_path(data_dir: Path) -> Path:
    """Marker holding the day (ISO date) the operator sent /stop, so a restart keeps it."""
    return data_dir / TRADING_OFF_FILE_NAME


def read_stopped_day(data_dir: Path) -> date | None:
    try:
        return date.fromisoformat(trading_off_path(data_dir).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def write_stopped_day(data_dir: Path, day: date | None) -> None:
    path = trading_off_path(data_dir)
    try:
        if day is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(day.isoformat() + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("could not update %s: %s", path.name, type(exc).__name__)


def should_stop(data_dir: Path, state: ServeState) -> bool:
    if state.stop_requested:
        return True
    return stop_file_path(data_dir).exists()


_CONTROL_KINDS = frozenset(
    {
        CommandKind.STOP_TODAY,
        CommandKind.START_TODAY,
        CommandKind.FLATTEN_CONFIRM,
        CommandKind.EXIT,
        CommandKind.REARM,
        CommandKind.RECONCILE,
        CommandKind.CLEARHALT,
        CommandKind.RESTART,
    }
)
_NO_SESSION = (
    "No session is running. This works while a session runs (about 09:00 to 15:30 IST on a "
    "market day)."
)


def _read_store(
    ctx: AppContext, reply: Callable[[str], None], build: Callable[[TradeStore], str]
) -> None:
    if not ctx.database_path.exists():
        reply("No database yet.")
        return
    store = TradeStore(ctx.database_path)
    try:
        reply(build(store))
    finally:
        store.close()


def build_funds_reader(ctx: AppContext) -> AccountReader:
    """A read-only view of the trading account, for /funds outside a session."""
    from orb_intraday_bot.adapters.groww.account import GrowwAccountReader
    from orb_intraday_bot.adapters.groww.factory import build_trading_client
    from orb_intraday_bot.adapters.groww.trading_client import AccountReadView

    return GrowwAccountReader(AccountReadView(build_trading_client(ctx.settings)))


def funds_reply(ctx: AppContext, state: ServeState) -> str:
    settings = ctx.settings
    risk = state.risk
    executor = state.runner.executor if state.runner is not None else None
    margin: float | None = None
    held: dict[str, int] | None = None
    held_label = ""
    note = "Available option margin: unknown (the funds lookup failed)"
    if executor is not None:
        margin = executor.available_margin()
        held = executor.held_quantities()
        held_label = "Broker positions" if executor.mode == "live" else "Dry-run ledger"
    elif settings.live_mode == "paper":
        note = "Available option margin: not read in paper mode (the account is never touched)"
    elif not settings.has_trading_credentials():
        note = "Available option margin: not read (no trading credentials configured)"
    else:
        try:
            margin = build_funds_reader(ctx).available_option_margin()
        except Exception as exc:  # noqa: BLE001 - a report command must always answer
            note = f"Available option margin: could not be read ({type(exc).__name__})"
    return ot.funds_text(
        capital_inr=risk.capital_inr,
        daily_loss_limit_inr=risk.limits.daily_loss_limit_inr(risk.capital_inr),
        mode_label=configured_mode_label(settings),
        margin=margin,
        margin_note=note,
        held=held,
        held_label=held_label,
    )


def _handle_report_command(
    command: Command,
    *,
    ctx: AppContext,
    state: ServeState,
    reply: Callable[[str], None],
    day: date,
) -> bool:
    """The read-only commands. True if `command` was one of them."""
    kind = command.kind
    runner = state.runner
    executor = runner.executor if runner is not None else None
    if kind is CommandKind.RISK:
        reply(ot.risk_text(state.risk, stopped_today=state.bus.is_stopped(day)))
    elif kind is CommandKind.MODE:
        reply(
            ot.mode_text(
                ctx.settings,
                state.risk,
                stopped_today=state.bus.is_stopped(day),
                executor_mode=executor.mode if executor is not None else None,
            )
        )
    elif kind is CommandKind.VERSION:
        reply(ot.version_text(ctx.cfg))
    elif kind is CommandKind.FUNDS:
        reply(funds_reply(ctx, state))
    elif kind is CommandKind.LOG:
        reply(ot.log_tail(ctx.data_dir, day, ot.parse_log_lines(command.arg)))
    elif kind is CommandKind.ORDERS:
        _read_store(ctx, reply, lambda store: ot.orders_text(store, day))
    elif kind is CommandKind.SIGNALS:
        _read_store(ctx, reply, lambda store: ot.signals_text(store, day))
    elif kind is CommandKind.PNL:
        period = ot.parse_pnl_period(command.arg)
        if period is None:
            reply("Usage: /pnl [day|week|month]")
        else:
            _read_store(ctx, reply, lambda store: ot.pnl_text(store, period, day))
    elif kind is CommandKind.PROGRESS:
        if runner is not None and runner.trading_day is not None:
            reply(ot.progress_text(runner.open_positions()))
        else:
            _read_store(ctx, reply, lambda store: ot.progress_text(store.open_positions()))
    elif kind is CommandKind.LEVELS:
        trading = runner.trading_day if runner is not None else None
        loaded = state.levels if state.levels_day == day else None
        last = trading.last_candle if trading is not None else None
        reply(
            ot.levels_text(
                loaded[0] if loaded else None,
                loaded[1] if loaded else None,
                trading.tracker.orh if trading is not None else None,
                trading.tracker.orl if trading is not None else None,
                last.close if last is not None else None,
            )
        )
    else:
        return False
    return True


def _handle_exit(
    command: Command, *, state: ServeState, reply: Callable[[str], None]
) -> None:
    """/exit <symbol> [code]: close one position. A real (live) sell needs the code."""
    runner = state.runner
    if runner is None:
        reply(_NO_SESSION)
        return
    open_ = runner.open_positions()
    listing = ", ".join(sorted({p.trading_symbol for p in open_})) or "none"
    parts = command.arg.split()
    if not parts:
        reply(f"Usage: /exit <symbol>. Open positions: {listing}")
        return
    matches = ot.match_positions(open_, parts[0])
    symbols = sorted({p.trading_symbol for p in matches})
    if not symbols:
        reply(f"No open position matches {parts[0]}. Open positions: {listing}")
        return
    if len(symbols) > 1:
        reply(f"{parts[0]} matches more than one position: {', '.join(symbols)}. Be more exact.")
        return
    symbol = symbols[0]
    action = f"exit:{symbol}"
    if any(p.order_mode == "live" for p in matches) and not state.bus.confirm(
        action, parts[1] if len(parts) > 1 else ""
    ):
        code = state.bus.begin_confirm(action)
        reply(f"This sends a real sell order for {symbol}. Confirm: /exit {symbol} {code}")
        return
    left = runner.exit_positions({p.position_id for p in matches})
    if left:
        reply(
            f"{symbol}: the sale did not complete and the position is still open. Check the "
            "warning above, then send /exit again or /flatten."
        )
    else:
        reply(f"{symbol}: closed (the exit message has the prices).")


def _handle_action_command(
    command: Command,
    *,
    ctx: AppContext,
    alerts: Alerts,
    state: ServeState,
    reply: Callable[[str], None],
) -> bool:
    """/exit, /rearm, /reconcile, /clearhalt and /restart. True if `command` was one of them."""
    kind = command.kind
    runner = state.runner
    if kind is CommandKind.EXIT:
        _handle_exit(command, state=state, reply=reply)
    elif kind in (CommandKind.REARM, CommandKind.RECONCILE):
        if runner is None:
            reply(_NO_SESSION)
        elif runner.executor is None:
            reply("This session places no orders (paper mode), so there is nothing to check.")
        elif kind is CommandKind.REARM:
            looked = runner.protect_open_positions()
            if not looked:
                reply("No open positions that hold orders.")
            for p in looked:
                reply(
                    f"{p.trading_symbol}: broker stop in place (trigger {p.stop_trigger:.2f}, "
                    f"qty {p.stop_quantity})"
                    if p.stop_order_id
                    else f"{p.trading_symbol}: the broker stop could not be placed (see warning)"
                )
        else:
            problems = runner.reconcile_now()
            if problems:
                reply(
                    "MISMATCH (new entries are blocked, nothing was sold):\n"
                    + "\n".join(problems)
                )
            else:
                reply(
                    f"The books match ({len(runner.open_positions())} open position(s) checked)."
                )
    elif kind is CommandKind.CLEARHALT:
        s = state.risk.state
        if not (s.halted or s.entries_blocked):
            reply("Nothing to clear: the kill switch is not tripped and entries are not blocked.")
        else:
            what = " and ".join(
                part
                for part in (
                    f"kill switch ({s.halt_reason})" if s.halted else "",
                    f"entry block ({s.block_reason})" if s.entries_blocked else "",
                )
                if part
            )
            if not state.bus.confirm("clearhalt", command.arg):
                code = state.bus.begin_confirm("clearhalt")
                reply(
                    f"Active: {what}. Check the broker first. To clear it and allow new "
                    f"entries: /clearhalt {code}"
                )
            else:
                state.risk.clear_halt()
                alerts.order_event(f"ORB bot: {what} cleared by the operator.")
                reply("Cleared. New entries are allowed again (the limits still apply).")
    elif kind is CommandKind.RESTART:
        if not state.bus.confirm("restart", command.arg):
            code = state.bus.begin_confirm("restart")
            reply(
                "This restarts the service (the supervisor starts it again within seconds). "
                "Open positions and their broker stops are kept and restored. "
                f"Confirm: /restart {code}"
            )
        else:
            alerts.order_event("ORB bot: restart requested by the operator.")
            reply("Restarting.")
            state.stop_requested = True
            if runner is not None:
                runner.request_stop()
    else:
        return False
    return True


def handle_command(
    command: Command,
    *,
    ctx: AppContext,
    alerts: Alerts,
    state: ServeState,
    reply: Callable[[str], None],
    now: datetime,
) -> None:
    """Apply one operator command. Read-only commands reply; control commands set flags."""
    bus = state.bus
    day = now.astimezone(IST).date()
    if command.kind in _CONTROL_KINDS:
        # An audit line for every command that changes something (never the confirmation code).
        log_event(log, "operator_command", command=command.kind.value)
    if _handle_report_command(command, ctx=ctx, state=state, reply=reply, day=day):
        return
    if _handle_action_command(command, ctx=ctx, alerts=alerts, state=state, reply=reply):
        return
    if command.kind is CommandKind.STATUS:
        from orb_intraday_bot.app.health import status_report

        report = status_report(ctx.data_dir / ctx.cfg.runtime.heartbeat_file, now)
        extra = ot.status_extra(
            ctx.settings,
            state.risk,
            stopped_today=bus.is_stopped(day),
            session_status=state.runner.status if state.runner is not None else None,
        )
        reply(report.text + "\n" + extra)
        return
    if command.kind is CommandKind.TODAY:
        if not ctx.database_path.exists():
            reply("No database yet.")
            return
        store = TradeStore(ctx.database_path)
        try:
            reply(day_summary_text(store, now.astimezone(IST).date()))
        finally:
            store.close()
        return
    if command.kind is CommandKind.POSITIONS:
        if not ctx.database_path.exists():
            reply("No open positions (no database).")
            return
        store = TradeStore(ctx.database_path)
        try:
            opens = store.open_positions()
        finally:
            store.close()
        if not opens:
            reply("No open positions.")
            return
        lines = [f"{len(opens)} open:"]
        for p in opens:
            lines.append(
                f"- {p.setup} {p.direction.value} {p.trading_symbol} "
                f"{p.remaining_lots} lot(s) @ {p.entry_ltp:.2f}"
            )
        reply("\n".join(lines))
        return
    if command.kind is CommandKind.REPORT:
        from orb_intraday_bot.app.forward import forward_report

        if not ctx.database_path.exists():
            reply("No database yet.")
            return
        store = TradeStore(ctx.database_path)
        try:
            reply(forward_report(store).text())
        finally:
            store.close()
        return
    if command.kind is CommandKind.STOP_TODAY:
        bus.stop_for(day)
        write_stopped_day(ctx.data_dir, day)
        alerts.paused(day.isoformat())
        reply(
            f"Trading stopped for {day.isoformat()}: no new entries. Open positions are still "
            "managed (use /flatten to close them). Send /start to allow trades again today; "
            "otherwise trading resumes on the next day by itself."
        )
        return
    if command.kind is CommandKind.START_TODAY:
        was_stopped = bus.is_stopped(day)
        bus.start_for(day)
        write_stopped_day(ctx.data_dir, None)
        alerts.resumed(day.isoformat())
        reply(
            f"Trading started for {day.isoformat()}: new entries allowed."
            if was_stopped
            else f"Trading was not stopped for {day.isoformat()}; new entries are already allowed."
        )
        return
    if command.kind is CommandKind.FLATTEN_CONFIRM:
        if bus.confirm_flatten(command.arg):
            stop_file_path(ctx.data_dir).parent.mkdir(parents=True, exist_ok=True)
            (ctx.data_dir / "FLATTEN").write_text("confirm\n", encoding="utf-8")
            alerts.flatten_requested()
            reply("Flatten requested. The session will close open positions.")
        else:
            reply("Flatten code mismatch. Send /flatten again for a new code.")
        return
    reply(f"Unhandled command: {command.kind.value}")


def drain_commands(
    ctx: AppContext,
    alerts: Alerts,
    state: ServeState,
    reply: Callable[[str], None],
    now: datetime,
) -> None:
    for command in state.bus.drain():
        try:
            handle_command(
                command, ctx=ctx, alerts=alerts, state=state, reply=reply, now=now
            )
        except Exception as exc:
            log.warning("Command %s failed: %s", command.kind.value, type(exc).__name__)
            reply(f"Command failed: {type(exc).__name__}: {exc}")


def run_one_session(
    ctx: AppContext,
    notifier: Notifier,
    state: ServeState,
    clock: Clock,
    sleep: SleepFunc,
) -> RunOutcome:
    """Run today's LiveRunner. Honours pause by wrapping allow_open at the day level later."""
    from orb_intraday_bot.analysis.levels import levels_path, remember

    store = TradeStore(ctx.database_path)
    data = build_market_data(ctx)

    def save_levels(symbol: str, levels: SessionLevels) -> None:
        state.levels = (symbol, levels)
        state.levels_day = today
        try:
            remember(levels_path(ctx.history_path(symbol)), levels)
        except OSError as exc:
            log.warning("could not save levels: %s", exc)

    flatten_marker = ctx.data_dir / "FLATTEN"
    today = clock.now().astimezone(IST).date()
    state.risk.reset_day(today)
    alerts = Alerts(notifier)
    restore_risk(state.risk, store, today)
    executor = build_executor(
        ctx, store=store, risk=state.risk, clock=clock, sleep=sleep, notify=alerts.order_event
    )

    def executor_enabled() -> bool:
        """The executor's order mode is the one the settings resolve to (no daily confirmation)."""
        if executor is None:
            return False
        decision = resolve_mode(
            ctx.settings,
            trading_credentials_present=ctx.settings.has_trading_credentials(),
        )
        return decision.order_mode == executor.mode

    def on_open(position: Position) -> None:
        state.risk.record_open()

    def on_close(position: Position) -> None:
        was_halted = state.risk.state.halted
        state.risk.record_close(position.net_option_pnl)
        if state.risk.state.halted and not was_halted:
            alerts.kill_switch(state.risk.state.halt_reason)

    def reply(text: str) -> None:
        notifier.send(text)

    def on_idle() -> None:
        drain_commands(ctx, alerts, state, reply, clock.now())

    if executor is not None and not executor_enabled() and ctx.settings.live_mode == "live":
        decision = resolve_mode(
            ctx.settings,
            trading_credentials_present=ctx.settings.has_trading_credentials(),
        )
        alerts.order_event(f"Running as PAPER: {decision.reason}.")
    def entries_allowed() -> bool:
        if state.bus.is_stopped(today) or state.risk.state.halted:
            return False
        decision = state.risk.check_entry(lots=1)
        return decision.allowed

    halt_marked = False

    def flatten_requested() -> bool:
        nonlocal halt_marked
        if flatten_marker.exists():
            return True
        if state.risk.state.halted:
            if not halt_marked:             # the marker survives a restart; write it once
                halt_marked = True
                flatten_marker.parent.mkdir(parents=True, exist_ok=True)
                flatten_marker.write_text(
                    f"risk:{state.risk.state.halt_reason}\n", encoding="utf-8"
                )
            return True
        return False

    runner = LiveRunner(
        ctx.cfg,
        ctx.data_dir,
        data,
        store,
        notifier,
        clock,
        sleep,
        make_output(ctx.data_dir, clock.now().date(), new_run_id(clock.now())),
        on_levels=save_levels,
        entries_allowed=entries_allowed,
        flatten_requested=flatten_requested,
        executor=executor,
        executor_enabled=executor_enabled,
        on_open=on_open,
        on_close=on_close,
        square_off_at=parse_clock_time(state.risk.limits.square_off_time),
        on_idle=on_idle,
    )
    state.runner = runner
    try:
        return runner.run()
    finally:
        state.runner = None
        store.close()


def parse_clock_time(text: str) -> dtime:
    hour, minute = (int(part) for part in text.split(":"))
    return dtime(hour, minute)


def restore_risk(risk: RiskEngine, store: TradeStore, day: date) -> None:
    """After a restart: today's trades and P&L come back from the database.

    Only positions handled by the executor (dry run or live) count; paper positions never did.
    """
    todays = [p for p in store.positions_for_day(day) if p.order_mode != "paper"]
    closed = sorted((p for p in todays if not p.is_open), key=lambda p: p.closed_at or p.opened_at)
    risk.rebuild(
        trades_today=len(todays),
        open_positions=sum(1 for p in todays if p.is_open),
        closed_pnls=[p.net_option_pnl for p in closed],
    )


def build_executor(
    ctx: AppContext,
    *,
    store: TradeStore,
    risk: RiskEngine,
    clock: Clock,
    sleep: SleepFunc,
    notify: Callable[[str], None],
) -> LiveExecutor | None:
    """The executor for this session, or None for plain paper mode.

    Real orders (live mode with DRY_RUN=false) go to Groww. Otherwise the dry-run venue fills
    against real bid/ask quotes and no order is ever sent. Everything else is identical.
    """
    from orb_intraday_bot.adapters.groww.account import GrowwAccountReader
    from orb_intraday_bot.adapters.groww.factory import (
        build_readonly_client,
        build_trading_client,
    )
    from orb_intraday_bot.adapters.groww.quotes import GrowwQuoteSource
    from orb_intraday_bot.adapters.groww.trading_client import AccountReadView
    from orb_intraday_bot.adapters.groww.venue import GrowwVenue
    from orb_intraday_bot.execution.account import DryRunAccount
    from orb_intraday_bot.execution.dry_run_venue import DryRunVenue
    from orb_intraday_bot.execution.quotes import QuoteSource
    from orb_intraday_bot.execution.venue import ExecutionVenue

    settings = ctx.settings
    if settings.live_mode == "paper":
        return None
    limits: RiskLimits = risk.limits
    quotes: QuoteSource
    venue: ExecutionVenue
    account: AccountReader
    if settings.live_mode == "live" and not settings.dry_run:
        mode = "live"
        client = build_trading_client(settings)
        quotes = GrowwQuoteSource(client, clock=clock)
        venue = GrowwVenue(
            client, clock, sleep, product=limits.product, fill_wait_sec=limits.order_poll_sec,
            fill_polls=0,
        )
        account = GrowwAccountReader(client)
    else:
        mode = "dry_run"
        quotes = GrowwQuoteSource(build_readonly_client(settings), clock=clock)
        dry_venue = DryRunVenue(quotes, clock)
        venue = dry_venue
        funds: AccountReader | None = None
        if settings.has_trading_credentials():
            funds = GrowwAccountReader(AccountReadView(build_trading_client(settings)))
        account = DryRunAccount(dry_venue, funds)
    return LiveExecutor(
        mode=mode, venue=venue, quotes=quotes, account=account, limits=limits, clock=clock,
        sleep=sleep, risk=risk, store=store, notify=notify,
    )


def post_session_report(ctx: AppContext, alerts: Alerts, day: date) -> None:
    if not ctx.database_path.exists():
        return
    store = TradeStore(ctx.database_path)
    try:
        summary = day_summary_text(store, day)
    finally:
        store.close()
    alerts.day_end(summary)
    try:
        text = reconcile_day(
            ctx.cfg, ctx.database_path, ctx.history_path, build_market_data(ctx), day, day
        )
        alerts.day_end(text)
    except NothingToDo as stop:
        log.info("reconcile skipped: %s", stop)
    except Exception as exc:
        log.warning("reconcile failed: %s", exc)
        alerts.day_end(f"Reconcile failed: {type(exc).__name__}: {exc}")


def serve_loop(
    ctx: AppContext,
    *,
    clock: Clock | None = None,
    sleep: SleepFunc | None = None,
    notifier: Notifier | None = None,
    session: SessionFunc | None = None,
    max_sessions: int | None = None,
    idle_step_sec: float = _IDLE_STEP_SEC,
    holidays: frozenset[date] | None = None,
) -> int:
    """The always-on loop. Returns 0 on graceful stop."""
    clock = clock or SystemClock()
    sleep = sleep or time.sleep
    notifier = notifier or build_notifier(ctx.settings, async_outbox=True)
    session = session or run_one_session
    alerts = Alerts(notifier)
    risk = RiskEngine(
        limits=load_risk_limits(),
        capital_inr=ctx.cfg.sizing.capital_inr,
    )
    state = ServeState(bus=CommandBus(), risk=risk)
    state.bus.stopped_day = read_stopped_day(ctx.data_dir)    # a /stop survives a restart
    holidays = load_holidays() if holidays is None else holidays
    mode = configured_mode_label(ctx.settings)

    def reply(text: str) -> None:
        notifier.send(text)

    poller: TelegramCommandPoller | None = build_command_poller(ctx.settings, state.bus, reply)
    if poller is not None:
        poller.start()

    alerts.startup(__version__, mode, fingerprint(ctx.cfg).strategy)
    sessions = 0
    exit_code = 0
    try:
        while not should_stop(ctx.data_dir, state):
            drain_commands(ctx, alerts, state, reply, clock.now())
            now = clock.now()
            start_at = next_session_start(now, start=SESSION_START, holidays=holidays)
            if now < start_at:
                log.info("Next session at %s", start_at.isoformat())
                while clock.now() < start_at and not should_stop(ctx.data_dir, state):
                    drain_commands(ctx, alerts, state, reply, clock.now())
                    remaining = (start_at - clock.now()).total_seconds()
                    sleep(max(min(remaining, idle_step_sec), 0.1))
                if should_stop(ctx.data_dir, state):
                    break

            if should_stop(ctx.data_dir, state):
                break
            day = clock.now().astimezone(IST).date()
            log.info("Starting session for %s", day.isoformat())
            outcome = session(ctx, notifier, state, clock, sleep)
            sessions += 1
            log.info("Session ended: %s (%s)", outcome.message, outcome.exit_code)
            if outcome.exit_code == 0:
                post_session_report(ctx, alerts, day)
            # Mid-day crash (exit_code 1): NSSM would restart the whole process; within this
            # loop we wait a short cooldown then try again if still before close.
            if outcome.exit_code == 1:
                alerts.stopped_with_error(outcome.message)
                sleep(30)
            if max_sessions is not None and sessions >= max_sessions:
                exit_code = outcome.exit_code
                break
    except KeyboardInterrupt:
        state.stop_requested = True
        exit_code = 130
    finally:
        if poller is not None:
            poller.stop()
        close = getattr(notifier, "close", None)
        if callable(close):
            close()
        stop = stop_file_path(ctx.data_dir)
        if stop.exists():
            with suppress(OSError):
                stop.unlink()
        alerts.shutdown("graceful" if exit_code in (0, 130) else f"exit {exit_code}")
    return exit_code


def main() -> int:  # pragma: no cover - process entrypoint
    ctx = load_context()
    ctx.data_dir.mkdir(parents=True, exist_ok=True)
    return serve_loop(ctx)
