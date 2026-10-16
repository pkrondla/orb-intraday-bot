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
from orb_intraday_bot.app.context import AppContext, build_market_data, load_context
from orb_intraday_bot.app.forward import NothingToDo, day_summary_text, reconcile_day
from orb_intraday_bot.config.fingerprint import fingerprint
from orb_intraday_bot.core.clock import IST, Clock, SystemClock
from orb_intraday_bot.paper.models import Position
from orb_intraday_bot.risk.engine import RiskEngine
from orb_intraday_bot.risk.limits import RiskLimits, load_risk_limits
from orb_intraday_bot.runtime.alerts import Alerts
from orb_intraday_bot.runtime.arming import (
    configured_mode_label,
    describe_order_mode,
    resolve_mode,
)
from orb_intraday_bot.runtime.calendar import load_holidays, next_session_start
from orb_intraday_bot.runtime.executor import LiveExecutor
from orb_intraday_bot.runtime.live import LiveRunner, RunOutcome
from orb_intraday_bot.runtime.logs import new_run_id
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
SESSION_START = "09:00"
_IDLE_STEP_SEC = 15


@dataclass
class ServeState:
    """Mutable flags the command poller and the runner share."""

    bus: CommandBus
    risk: RiskEngine
    stop_requested: bool = False


SleepFunc = Callable[[float], None]
OutFunc = Callable[[str], None]
SessionFunc = Callable[[AppContext, Notifier, ServeState, Clock, SleepFunc], RunOutcome]


def stop_file_path(data_dir: Path) -> Path:
    return data_dir / STOP_FILE_NAME


def should_stop(data_dir: Path, state: ServeState) -> bool:
    if state.stop_requested:
        return True
    return stop_file_path(data_dir).exists()


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
    if command.kind is CommandKind.STATUS:
        from orb_intraday_bot.app.health import status_report

        report = status_report(ctx.data_dir / ctx.cfg.runtime.heartbeat_file, now)
        reply(report.text)
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
    if command.kind is CommandKind.PAUSE:
        bus.paused = True
        alerts.paused()
        reply("Paused.")
        return
    if command.kind is CommandKind.RESUME:
        bus.paused = False
        alerts.resumed()
        reply("Resumed.")
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
    if command.kind is CommandKind.ARM:
        if bus.confirm_arm(command.arg, now.astimezone(IST).date()):
            today = now.astimezone(IST).date()
            decision = resolve_mode(
                ctx.settings,
                armed_today=bus.is_armed(today),
                trading_credentials_present=ctx.settings.has_trading_credentials(),
            )
            alerts.armed(describe_order_mode(decision.order_mode))
            reply(f"Armed for today. Orders: {describe_order_mode(decision.order_mode)}.")
        else:
            # First /arm without a pending code, or wrong code: issue a new one.
            code = bus.begin_arm()
            reply(f"To arm live trading today, reply /arm {code}")
        return
    if command.kind is CommandKind.DISARM:
        bus.disarm()
        alerts.disarmed()
        reply("Disarmed.")
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
    from orb_intraday_bot.indicators.pivots import SessionLevels

    store = TradeStore(ctx.database_path)
    data = build_market_data(ctx)

    def save_levels(symbol: str, levels: SessionLevels) -> None:
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
        """Armed today and the executor's order mode is the one the arming resolves to."""
        if executor is None:
            return False
        decision = resolve_mode(
            ctx.settings,
            armed_today=state.bus.is_armed(clock.now().astimezone(IST).date()),
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
            armed_today=False,
            trading_credentials_present=ctx.settings.has_trading_credentials(),
        )
        alerts.order_event(
            f"Not armed today: running as PAPER. {decision.reason}. "
            "Send /arm and confirm the code to enable orders."
        )

    def entries_allowed() -> bool:
        if state.bus.paused or state.risk.state.halted:
            return False
        decision = state.risk.check_entry(lots=1)
        return decision.allowed

    def flatten_requested() -> bool:
        if flatten_marker.exists():
            return True
        if state.risk.state.halted:
            flatten_marker.parent.mkdir(parents=True, exist_ok=True)
            flatten_marker.write_text(f"risk:{state.risk.state.halt_reason}\n", encoding="utf-8")
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
    try:
        return runner.run()
    finally:
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
    from orb_intraday_bot.execution.account import AccountReader, DryRunAccount
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
