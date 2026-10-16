"""The live session: one trading day, started once (for example at 09:10 IST).

    start -> wait for the open -> every 3 minutes fetch the closed candle -> report -> ...
          -> 15:30 end of day, summary, exit

What it does, in plain terms:
- It polls Groww over REST (the streaming feed does not connect from this machine). A candle is
  fetched a few seconds after its boundary and retried until it is published.
- Whatever candles it finds that it has not processed are processed in order. So a late start, a
  restart, or a stall are all the same thing: it catches up, rebuilds the day's patterns from the
  candles, restores open positions from the database, and applies the missed candles to them. A
  signal that fired while it was not running is shown as MISSED and never entered late.
- While a position is open it polls the futures and option prices every few seconds, so stops and
  targets are checked on live prices as well as at each candle close.
- Errors fetching data are retried and alerted (watchdog). An error in the strategy code stops the
  run (positions are saved; restart to continue).
- Paper mode never places an order. With an executor (the dry run, or live trading when armed
  today) each entry and exit goes through the same order pipeline; only the final broker call
  differs. At the square-off time (15:20) those positions are closed and no entry is opened.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Protocol

from orb_intraday_bot import __version__
from orb_intraday_bot.config.fingerprint import fingerprint
from orb_intraday_bot.config.settings import StrategyConfig
from orb_intraday_bot.core.clock import IST, Clock
from orb_intraday_bot.core.errors import must_halt
from orb_intraday_bot.core.models import Candle
from orb_intraday_bot.data.contract import FuturesContract
from orb_intraday_bot.indicators.pivots import SessionLevels
from orb_intraday_bot.options.planner import OptionDataSource, OptionPlanner
from orb_intraday_bot.paper.models import ExitReason, Position
from orb_intraday_bot.runtime.alerts import Alerts
from orb_intraday_bot.runtime.arming import describe_order_mode
from orb_intraday_bot.runtime.day import TradingDay
from orb_intraday_bot.runtime.executor import LiveExecutor
from orb_intraday_bot.runtime.logs import log_event
from orb_intraday_bot.runtime.monitor import (
    Watchdog,
    another_instance_running,
    write_heartbeat,
)
from orb_intraday_bot.runtime.notify import Notifier
from orb_intraday_bot.runtime.report import format_candle_report, format_day_end, format_exit
from orb_intraday_bot.storage.store import TradeStore

EntriesAllowed = Callable[[], bool]
FlattenRequested = Callable[[], bool]
PositionHook = Callable[[Position], None]

log = logging.getLogger("orb.live")
_CANDLE = timedelta(minutes=3)
_IDLE_STEP_SEC = 20          # longest sleep between heartbeats when nothing is open


class LiveDataSource(OptionDataSource, Protocol):
    def futures_contract(self, today: date | None = None) -> FuturesContract: ...

    def previous_session_levels(
        self, contract: FuturesContract, today: date, lookback_days: int = 10
    ) -> SessionLevels: ...

    def session_candles(self, contract: FuturesContract, day: date) -> list[Candle]: ...

    def ltp(self, trading_symbols: Sequence[str]) -> dict[str, float]: ...


@dataclass(frozen=True, slots=True)
class RunOutcome:
    exit_code: int
    message: str


class LiveRunner:
    def __init__(
        self,
        config: StrategyConfig,
        data_dir: Path,
        source: LiveDataSource,
        store: TradeStore,
        notifier: Notifier,
        clock: Clock,
        sleep: Callable[[float], None],
        out: Callable[[str], None],
        on_levels: Callable[[str, SessionLevels], None] | None = None,
        entries_allowed: EntriesAllowed | None = None,
        flatten_requested: FlattenRequested | None = None,
        *,
        executor: LiveExecutor | None = None,
        executor_enabled: Callable[[], bool] | None = None,
        on_open: PositionHook | None = None,
        on_close: PositionHook | None = None,
        square_off_at: time | None = None,
        on_idle: Callable[[], None] | None = None,
    ) -> None:
        self._cfg = config
        self._on_idle = on_idle
        self._executor = executor
        self._executor_enabled = executor_enabled or (lambda: executor is not None)
        self._on_open = on_open
        self._on_close = on_close
        self._square_off_at = square_off_at
        self._on_levels = on_levels
        self._entries_allowed = entries_allowed or (lambda: True)
        self._flatten_requested = flatten_requested or (lambda: False)
        self._source = source
        self._store = store
        self._alerts = Alerts(notifier, config.runtime.notify_candle_reports)
        self._clock = clock
        self._sleep = sleep
        self._out = out
        self._data_dir = data_dir
        self._heartbeat = data_dir / config.runtime.heartbeat_file
        self._watchdog = Watchdog(notifier, config.runtime.stall_alert_sec)
        self._day: TradingDay | None = None
        self._contract: FuturesContract | None = None
        self._last_poll: datetime | None = None
        self._status = "starting"
        self._flattened = False
        self._flatten_announced = False

    # -- entry point ---------------------------------------------------------------------
    def run(self) -> RunOutcome:
        now = self._clock.now()
        if another_instance_running(self._heartbeat, now):
            return RunOutcome(3, "Another run is alive (fresh heartbeat). Not starting a second.")
        try:
            return self._run_day(now.date())
        except KeyboardInterrupt:
            self._beat("stopped", "stopped by the user")
            self._out(
                "Stopped. Open positions stay in the database and are restored on the next run."
            )
            return RunOutcome(130, "stopped by the user")
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            log.exception("Run failed", extra={"event": "run_failed", "error": msg})
            self._beat("error", msg)
            self._alerts.stopped_with_error(msg)
            return RunOutcome(1, msg)

    # -- the day -------------------------------------------------------------------------
    def _run_day(self, day: date) -> RunOutcome:
        cfg = self._cfg
        self._beat("starting", "loading the contract and pivots")
        contract = self._source.futures_contract(day)
        levels = self._source.previous_session_levels(contract, day)
        pivots = levels.pivots()
        if self._on_levels is not None:
            try:
                self._on_levels(contract.trading_symbol, levels)
            except OSError as exc:            # a record for replay; never worth stopping a run
                log.warning("could not save the pivot inputs: %s", exc)
        self._contract = contract
        trading = TradingDay(
            cfg, day, contract.trading_symbol, pivots, OptionPlanner(cfg, self._source),
            self._store,
            executor=self._executor,
            executor_enabled=self._executor_enabled,
            on_open=self._on_open,
            on_close=self._on_close,
        )
        self._day = trading
        self._start_executor(trading)
        self._out(self._banner(contract, levels, pivots.r1, pivots.s1, trading))
        log_event(
            log, "run_started", day=day.isoformat(), symbol=contract.trading_symbol,
            version=__version__, strategy_hash=fingerprint(cfg).strategy,
            restored_positions=len(trading.paper.open_positions),
        )

        open_dt = datetime.combine(day, time(9, 15), tzinfo=IST)
        close_dt = datetime.combine(day, time(15, 30), tzinfo=IST)
        h, m = (int(x) for x in cfg.runtime.no_session_exit_time.split(":"))
        no_session_at = datetime.combine(day, time(h, m), tzinfo=IST)
        delay = timedelta(seconds=cfg.session.candle_publish_delay_sec)

        last_start: datetime | None = None
        first_due = open_dt + _CANDLE + delay
        if self._clock.now() < first_due:
            self._beat("waiting", f"waiting for the first candle at {first_due:%H:%M:%S}")
            self._out(f"Waiting for the market: first candle closes {open_dt + _CANDLE:%H:%M}.")
            self._wait_until(first_due)

        while True:
            now = self._clock.now()
            candles = self._fetch(contract, day)
            if candles is not None:
                if not candles and now >= no_session_at:
                    self._beat("finished", "no session data today")
                    msg = f"No candles by {no_session_at:%H:%M}: market holiday? Nothing to do."
                    self._out(msg)
                    return RunOutcome(0, msg)
                fresh = [c for c in candles if last_start is None or c.start > last_start]
                for candle in fresh:
                    self._process(trading, candle)
                    last_start = candle.start
                    self._watchdog.candle_arrived()

            if last_start is not None and last_start + _CANDLE >= close_dt:
                return self._finish(trading, day)
            if now >= close_dt + timedelta(minutes=5):
                # The last candle never arrived. Close at the last known price, end the run.
                self._force_end_of_day(trading, close_dt)
                return self._finish(trading, day)

            due = (last_start + 2 * _CANDLE if last_start else first_due) + delay
            now = self._clock.now()
            if now >= due:
                self._watchdog.check_late(now, due, last_start)
                self._beat("running", f"waiting for a late candle (due {due:%H:%M:%S})")
                self._wait_until(now + timedelta(seconds=cfg.runtime.fetch_retry_sec))
            else:
                self._beat("running", f"next candle due {due:%H:%M:%S}")
                self._wait_until(due)

    # -- pieces --------------------------------------------------------------------------
    def _fetch(self, contract: FuturesContract, day: date) -> list[Candle] | None:
        try:
            candles = self._source.session_candles(contract, day)
        except Exception as exc:         # data problems are retried, not fatal
            if must_halt(exc):
                raise
            msg = self._watchdog.record_error(exc)
            log.warning("Candle fetch failed: %s", msg)
            return None
        self._watchdog.record_success()
        return candles

    def _process(self, trading: TradingDay, candle: Candle) -> None:
        self._maybe_flatten(trading)
        now = self._clock.now()
        age = (now - (candle.start + _CANDLE)).total_seconds()
        fresh = age <= self._cfg.runtime.max_signal_age_sec
        allow = fresh and self._entries_allowed() and not self._past_square_off()
        report = trading.on_candle(candle, allow_open=allow, catch_up=not fresh)
        self._maybe_square_off(trading)
        self._out(format_candle_report(report))
        self._alerts.candle(report)
        log_event(
            log, "candle_processed", candle=f"{candle.start:%H:%M}", fresh=fresh,
            opened=len(report.opened), exits=len(report.exits),
            open_positions=len(trading.paper.open_positions),
        )
        self._beat("running", f"processed {candle.start:%H:%M}")

    def _finish(self, trading: TradingDay, day: date) -> RunOutcome:
        text = format_day_end(trading.summary(), trading.positions_today())
        self._out(text)
        self._alerts.day_end(text)
        log_event(
            log, "run_finished", day=day.isoformat(), trades=len(trading.positions_today()),
        )
        left = trading.paper.open_positions
        if left:
            symbols = ", ".join(sorted({p.trading_symbol for p in left}))
            self._alerts.order_event(
                f"CRITICAL: the session ended with {len(left)} position(s) still open "
                f"({symbols}). Check the broker and close them by hand."
            )
        self._beat("finished", "session complete")
        return RunOutcome(0, "session complete")

    def _force_end_of_day(self, trading: TradingDay, close_dt: datetime) -> None:
        if not trading.paper.open_positions:
            return
        price = trading.last_candle.close if trading.last_candle else None
        options: dict[str, float] = {}
        contract = self._contract
        if contract is not None:
            try:
                symbols = [contract.trading_symbol] + sorted(
                    {p.trading_symbol for p in trading.paper.open_positions}
                )
                live = self._source.ltp(symbols)
                price = live.get(contract.trading_symbol, price)
                options = {s: v for s, v in live.items() if s != contract.trading_symbol}
            except Exception as exc:
                if must_halt(exc):
                    raise
                log.warning("End-of-day price fetch failed: %s", exc)
        if price is None:
            return
        self._out("  The last candle never arrived: closing open positions at the last price.")
        for event in trading.paper.close_all(close_dt, price, options):
            self._out("  " + format_exit(event))
            self._alerts.exit(event)

    def _maybe_flatten(self, trading: TradingDay) -> None:
        if self._flattened or not self._flatten_requested():
            return
        if not trading.paper.open_positions:
            self._flattened = True
            return
        if not self._flatten_announced:
            self._flatten_announced = True
            self._out("Flatten requested: closing open positions.")
            self._alerts.flatten_requested()
        # With real or dry-run orders a sale can fail; the flatten then stays pending and is
        # retried on the next poll instead of being marked done.
        if not self._close_open(trading, ExitReason.FLATTEN):
            return
        self._flattened = True
        marker = self._data_dir / "FLATTEN"
        if marker.exists():
            with suppress(OSError):
                marker.unlink()

    def _past_square_off(self) -> bool:
        """True from the square-off time on, while orders are being carried out (not paper)."""
        if self._executor is None or self._square_off_at is None or not self._executor_enabled():
            return False
        return self._clock.now().astimezone(IST).time() >= self._square_off_at

    def _maybe_square_off(self, trading: TradingDay) -> None:
        """At the square-off time close every position that holds real or dry-run orders.

        Paper positions are left to the normal 15:30 close. A failed sale is retried on the
        next poll, and `_finish` raises an alert if anything is still open at the end.
        """
        if not self._past_square_off() or not self._executed_open(trading):
            return
        self._close_open(trading, ExitReason.SQUARE_OFF, executed_only=True)

    @staticmethod
    def _executed_open(trading: TradingDay) -> list[Position]:
        return [p for p in trading.paper.open_positions if p.order_mode != "paper"]

    def _close_open(
        self, trading: TradingDay, reason: ExitReason, *, executed_only: bool = False
    ) -> bool:
        """Close positions at the latest prices. True when none of them remain open."""
        price = trading.last_candle.close if trading.last_candle else None
        options: dict[str, float] = {}
        contract = self._contract
        if contract is not None and price is not None:
            try:
                symbols = [contract.trading_symbol] + sorted(
                    {p.trading_symbol for p in trading.paper.open_positions}
                )
                live = self._source.ltp(symbols)
                price = live.get(contract.trading_symbol, price)
                options = {s: v for s, v in live.items() if s != contract.trading_symbol}
            except Exception as exc:
                if must_halt(exc):
                    raise
                log.warning("%s price fetch failed: %s", reason.value, exc)
        if price is None:
            return False
        for event in trading.paper.close_all(
            self._clock.now(), price, options, reason, executed_only
        ):
            self._out("  " + format_exit(event))
            self._alerts.exit(event)
        remaining = (
            self._executed_open(trading) if executed_only else trading.paper.open_positions
        )
        return not remaining

    def _start_executor(self, trading: TradingDay) -> None:
        """At session start: re-arm protective stops and check the books after a restart."""
        executor = self._executor
        if executor is None:
            return
        try:
            executor.startup(trading.paper.open_positions, self._clock.now())
        except Exception as exc:
            if must_halt(exc):
                raise
            log.warning("executor startup failed: %s", exc, exc_info=True)

    def _reconcile(self, trading: TradingDay) -> None:
        executor = self._executor
        if executor is None:
            return
        positions = trading.paper.open_positions
        if not (self._executor_enabled() or any(p.order_mode == executor.mode for p in positions)):
            return
        try:
            executor.maybe_reconcile(positions, self._clock.now())
        except Exception as exc:
            if must_halt(exc):
                raise
            log.warning("reconciliation failed: %s", exc, exc_info=True)

    def _poll_ticks(self) -> None:
        trading, contract = self._day, self._contract
        if trading is None or contract is None:
            return
        self._maybe_flatten(trading)
        self._maybe_square_off(trading)
        self._reconcile(trading)
        positions = trading.paper.open_positions
        if not positions:
            return
        now = self._clock.now()
        interval = self._cfg.runtime.poll_interval_sec
        if self._last_poll is not None and (now - self._last_poll).total_seconds() < interval:
            return
        self._last_poll = now
        symbols = [contract.trading_symbol] + sorted({p.trading_symbol for p in positions})
        try:
            prices = self._source.ltp(symbols)
        except Exception as exc:
            if must_halt(exc):
                raise
            log.warning("Price poll failed: %s", self._watchdog.record_error(exc))
            return
        self._watchdog.record_success()
        futures = prices.get(contract.trading_symbol)
        if futures is None:
            return
        options = {s: v for s, v in prices.items() if s != contract.trading_symbol}
        for event in trading.paper.on_tick(now, futures, options):
            self._out("  " + format_exit(event))
            self._alerts.exit(event)

    def _wait_until(self, target: datetime) -> None:
        interval = self._cfg.runtime.poll_interval_sec
        while True:
            now = self._clock.now()
            if now >= target:
                return
            if self._on_idle is not None:
                self._on_idle()          # operator commands (/arm, /pause, /flatten) mid-session
            self._poll_ticks()
            remaining = (target - self._clock.now()).total_seconds()
            if remaining <= 0:
                return
            has_open = bool(self._day and self._day.paper.open_positions)
            step = min(remaining, interval if has_open else _IDLE_STEP_SEC)
            self._sleep(max(step, 0.5))
            self._beat(self._status_or("running"), "")

    def _status_or(self, default: str) -> str:
        return self._status if self._status in {"waiting", "running"} else default

    def _beat(self, status: str, message: str) -> None:
        self._status = status
        trading = self._day
        write_heartbeat(
            self._heartbeat,
            now=self._clock.now(),
            status=status,
            last_candle=trading.last_candle.start if trading and trading.last_candle else None,
            open_positions=len(trading.paper.open_positions) if trading else 0,
            errors=self._watchdog.errors,
            message=message,
        )

    def _banner(
        self,
        contract: FuturesContract,
        levels: SessionLevels,
        r1: float,
        s1: float,
        trading: TradingDay,
    ) -> str:
        restored = trading.paper.open_positions
        fp = fingerprint(self._cfg)
        executor = self._executor
        mode_label = (
            "PAPER MODE (no orders are ever placed)"
            if executor is None
            else f"{describe_order_mode(executor.mode)} when armed today, paper otherwise"
        )
        lines = [
            f"ORB Intraday Bot v{__version__} - {mode_label}",
            f"  {contract.trading_symbol} (expiry {contract.expiry}, lot {contract.lot_size}); "
            f"pivots from {levels.day} ({levels.describe()}): S1 {s1:.1f}, R1 {r1:.1f}",
            f"  rules {fp.strategy} (assumptions {fp.assumptions}, runtime {fp.runtime})",
        ]
        if restored:
            lines.append(f"  restored {len(restored)} open position(s) from the database")
        return "\n".join(lines)
