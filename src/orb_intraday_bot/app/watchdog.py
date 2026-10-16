"""Independent dead-man monitor.

A separate process from `orb-bot serve`. During market hours it reads the heartbeat; if the
heartbeat is stale or the status is `error`, it alerts on Telegram. It never places orders
(confirmed: alert only; broker-side stops protect live positions).
"""

from __future__ import annotations

import logging
import time
import urllib.request
from collections.abc import Callable
from datetime import date, datetime
from datetime import time as dt_time

from orb_intraday_bot.app.context import AppContext, load_context
from orb_intraday_bot.core.clock import IST, Clock, SystemClock
from orb_intraday_bot.runtime.alerts import Alerts
from orb_intraday_bot.runtime.calendar import is_trading_day, load_holidays
from orb_intraday_bot.runtime.monitor import ACTIVE_STATUSES, ALIVE_WITHIN_SEC, read_heartbeat
from orb_intraday_bot.runtime.notify import Notifier, build_notifier

log = logging.getLogger("orb.watchdog")
MARKET_OPEN = dt_time(9, 15)
MARKET_CLOSE = dt_time(15, 35)
POLL_SEC = 30
# How long after a stale heartbeat before we alert (slightly above ALIVE_WITHIN_SEC).
STALE_SEC = ALIVE_WITHIN_SEC + 30
COOLDOWN_SEC = 300          # do not re-alert more often than this


SleepFunc = Callable[[float], None]
PingFunc = Callable[[str], None]


def _urllib_ping(url: str) -> None:  # pragma: no cover - network
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=10) as response:   # noqa: S310
        response.read()


def in_market_hours(now: datetime) -> bool:
    local = now.astimezone(IST)
    return MARKET_OPEN <= local.time() <= MARKET_CLOSE


def check_once(
    ctx: AppContext,
    alerts: Alerts,
    now: datetime,
    *,
    holidays: frozenset[date] | None = None,
    ping: PingFunc | None = None,
    last_alerted_at: datetime | None = None,
) -> datetime | None:
    """Return the new last_alerted_at (or the previous one if nothing was sent)."""
    day = now.astimezone(IST).date()
    closed = holidays if holidays is not None else load_holidays()
    if not is_trading_day(day, closed) or not in_market_hours(now):
        return last_alerted_at

    info = read_heartbeat(ctx.data_dir / ctx.cfg.runtime.heartbeat_file)
    silent = False
    message = ""
    age: float = 0.0
    status = "missing"
    if info is None:
        silent = True
        message = "No heartbeat file."
        age = float(STALE_SEC)
    else:
        age = info.age_sec(now)
        status = info.status
        if status == "error":
            silent = True
            message = info.message or "status=error"
        elif status in ACTIVE_STATUSES and age > STALE_SEC:
            silent = True
            message = info.message or f"last status={status}"
        elif (
            status == "finished"
            and in_market_hours(now)
            and now.astimezone(IST).time() < dt_time(15, 25)
        ):
            # Finished too early on a trading day.
            silent = True
            message = "Session finished before market close."

    if silent:
        if last_alerted_at is None or (now - last_alerted_at).total_seconds() >= COOLDOWN_SEC:
            alerts.silent(age, status, message)
            last_alerted_at = now
    else:
        # Healthy: optional external healthcheck ping.
        url = getattr(ctx.settings, "healthcheck_url", "") or ""
        if url and ping is not None:
            try:
                ping(url)
            except Exception as exc:
                log.warning("Healthcheck ping failed: %s", type(exc).__name__)
    return last_alerted_at


def watchdog_loop(
    ctx: AppContext,
    *,
    clock: Clock | None = None,
    sleep: SleepFunc | None = None,
    notifier: Notifier | None = None,
    ping: PingFunc | None = None,
    max_iterations: int | None = None,
    poll_sec: float = POLL_SEC,
) -> int:
    clock = clock or SystemClock()
    sleep = sleep or time.sleep
    notifier = notifier or build_notifier(ctx.settings, async_outbox=False)
    alerts = Alerts(notifier)
    holidays = load_holidays()
    last_alerted: datetime | None = None
    iterations = 0
    try:
        while True:
            last_alerted = check_once(
                ctx, alerts, clock.now(), holidays=holidays, ping=ping or _urllib_ping,
                last_alerted_at=last_alerted,
            )
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                return 0
            sleep(poll_sec)
    except KeyboardInterrupt:
        return 130


def main() -> int:  # pragma: no cover - process entrypoint
    ctx = load_context()
    return watchdog_loop(ctx)
