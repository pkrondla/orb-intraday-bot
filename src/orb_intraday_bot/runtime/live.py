"""The live paper session: one trading day, started once (for example at 09:10 IST).

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
- It never places an order. There is no order code in the project.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
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
from orb_intraday_bot.runtime.alerts import Alerts
from orb_intraday_bot.runtime.day import TradingDay
from orb_intraday_bot.runtime.logs import log_event
from orb_intraday_bot.runtime.monitor import (
    Watchdog,
    another_instance_running,
    write_heartbeat,
)
from orb_intraday_bot.runtime.notify import Notifier
from orb_intraday_bot.runtime.report import format_candle_report, format_day_end, format_exit
from orb_intraday_bot.storage.store import TradeStore

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
    ) -> None:
        self._cfg = config
        self._on_levels = on_levels
        self._source = source
        self._store = store
        self._alerts = Alerts(notifier, config.runtime.notify_candle_reports)
        self._clock = clock
        self._sleep = sleep
        self._out = out
        self._heartbeat = data_dir / config.runtime.heartbeat_file
        self._watchdog = Watchdog(notifier, config.runtime.stall_alert_sec)
        self._day: TradingDay | None = None
        self._contract: FuturesContract | None = None
        self._last_poll: datetime | None = None
        self._status = "starting"

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
        )
        self._day = trading
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
        now = self._clock.now()
        age = (now - (candle.start + _CANDLE)).total_seconds()
        fresh = age <= self._cfg.runtime.max_signal_age_sec
        report = trading.on_candle(candle, allow_open=fresh, catch_up=not fresh)
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

    def _poll_ticks(self) -> None:
        trading, contract = self._day, self._contract
        if trading is None or contract is None:
            return
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
        lines = [
            f"ORB Intraday Bot v{__version__} - PAPER MODE (no orders are ever placed)",
            f"  {contract.trading_symbol} (expiry {contract.expiry}, lot {contract.lot_size}); "
            f"pivots from {levels.day} ({levels.describe()}): S1 {s1:.1f}, R1 {r1:.1f}",
            f"  rules {fp.strategy} (assumptions {fp.assumptions}, runtime {fp.runtime})",
        ]
        if restored:
            lines.append(f"  restored {len(restored)} open position(s) from the database")
        return "\n".join(lines)
