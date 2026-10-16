"""Heartbeat file and stall watchdog.

The runner writes `heartbeat.json` (inside DATA_DIR) every few seconds: the status, the last
candle processed, how many positions are open and how many errors it has seen. It is used for:

- `orb-bot status`: shows whether a run is alive and what it is doing
- refusing to start a second run while one is alive (two runs would write the same database)
- an external monitor (Task Scheduler, a script): a heartbeat older than a minute or two while
  the status is "running" means the process is stuck or dead

The watchdog inside the runner raises an alert, once per episode, when the market data stops
(no new candle long after it should exist) or keeps failing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from orb_intraday_bot.runtime.notify import Notifier

ACTIVE_STATUSES = frozenset({"starting", "waiting", "running"})
ALIVE_WITHIN_SEC = 120
CONSECUTIVE_ERRORS_ALERT = 3


@dataclass(frozen=True, slots=True)
class HeartbeatInfo:
    status: str
    time: datetime
    last_candle: str | None
    open_positions: int
    errors: int
    message: str
    pid: int

    def age_sec(self, now: datetime) -> float:
        return (now - self.time).total_seconds()


def write_heartbeat(
    path: Path,
    *,
    now: datetime,
    status: str,
    last_candle: datetime | None,
    open_positions: int,
    errors: int,
    message: str = "",
) -> None:
    payload: dict[str, Any] = {
        "status": status,
        "time": now.isoformat(),
        "last_candle": last_candle.isoformat() if last_candle else None,
        "open_positions": open_positions,
        "errors": errors,
        "message": message,
        "pid": os.getpid(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)           # atomic: a reader never sees a half-written file


def read_heartbeat(path: Path) -> HeartbeatInfo | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return HeartbeatInfo(
            status=str(raw["status"]),
            time=datetime.fromisoformat(raw["time"]),
            last_candle=raw.get("last_candle"),
            open_positions=int(raw.get("open_positions", 0)),
            errors=int(raw.get("errors", 0)),
            message=str(raw.get("message", "")),
            pid=int(raw.get("pid", 0)),
        )
    except (OSError, ValueError, KeyError):
        return None


def another_instance_running(path: Path, now: datetime) -> bool:
    """A fresh heartbeat in an active status means another run is alive."""
    info = read_heartbeat(path)
    return (
        info is not None
        and info.status in ACTIVE_STATUSES
        and info.pid != os.getpid()
        and 0 <= info.age_sec(now) < ALIVE_WITHIN_SEC
    )


class Watchdog:
    def __init__(self, notifier: Notifier, stall_alert_sec: int) -> None:
        self._notifier = notifier
        self._stall_sec = stall_alert_sec
        self.errors = 0
        self._consecutive = 0
        self._alerted_error = False
        self._alerted_stall = False
        self.last_error = ""

    def record_success(self) -> None:
        self._consecutive = 0
        self._alerted_error = False

    def record_error(self, exc: Exception) -> str:
        self.errors += 1
        self._consecutive += 1
        self.last_error = f"{type(exc).__name__}: {exc}"
        if self._consecutive >= CONSECUTIVE_ERRORS_ALERT and not self._alerted_error:
            self._alerted_error = True
            self._notifier.send(
                f"ORB bot: market data failing ({self._consecutive} errors in a row). "
                f"Last: {self.last_error}"
            )
        return self.last_error

    def candle_arrived(self) -> None:
        self._alerted_stall = False

    def check_late(self, now: datetime, due: datetime, last_candle: datetime | None) -> bool:
        """Alert once if the expected candle is more than the stall limit late."""
        late = (now - due).total_seconds()
        if late < self._stall_sec or self._alerted_stall:
            return False
        self._alerted_stall = True
        since = f"{last_candle:%H:%M}" if last_candle else "the open"
        self._notifier.send(
            f"ORB bot: no new candle for {late / 60:.0f} min (last processed: {since}). "
            f"Open positions are not being checked against candles until data returns."
        )
        return True
