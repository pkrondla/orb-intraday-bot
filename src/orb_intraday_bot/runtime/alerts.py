"""What the bot tells the operator, and when. The runner decides nothing about wording.

All messages go through one object so the rules (which events alert, what they say) live in one
place and a second channel is a change here only.
"""

from __future__ import annotations

from orb_intraday_bot.paper.exits import ExitEvent
from orb_intraday_bot.runtime.day import CandleReport
from orb_intraday_bot.runtime.notify import Notifier
from orb_intraday_bot.runtime.report import format_candle_report, format_exit, format_opened


class Alerts:
    def __init__(self, notifier: Notifier, every_candle: bool = False) -> None:
        self._notifier = notifier
        self._every_candle = every_candle

    def candle(self, report: CandleReport) -> None:
        """Opened positions, missed signals, exits, and (if enabled) the whole candle report."""
        for outcome in report.opened:
            self._notifier.send(format_opened(outcome))
        for outcome in report.outcomes:
            if outcome.status == "stale":
                s = outcome.signal
                self._notifier.send(
                    f"MISSED {s.setup.value} {s.level_name} {s.direction.value} @ "
                    f"{s.entry_price:.1f}: the bot was not running when it fired."
                )
        for event in report.exits:
            self.exit(event)
        if self._every_candle:
            self._notifier.send(format_candle_report(report))

    def exit(self, event: ExitEvent) -> None:
        self._notifier.send(format_exit(event))

    def day_end(self, text: str) -> None:
        self._notifier.send(text)

    def stopped_with_error(self, message: str) -> None:
        self._notifier.send(f"ORB bot STOPPED with an error: {message}\nPositions are saved.")

    def startup(self, version: str, mode: str, strategy_hash: str) -> None:
        self._notifier.send(
            f"ORB bot STARTED v{version}\nmode: {mode}\nrules: {strategy_hash}"
        )

    def shutdown(self, reason: str = "stopped") -> None:
        self._notifier.send(f"ORB bot STOPPED: {reason}")

    def session_start(self, banner: str) -> None:
        self._notifier.send(banner)

    def paused(self) -> None:
        self._notifier.send("ORB bot PAUSED: no new entries. Open positions still managed.")

    def resumed(self) -> None:
        self._notifier.send("ORB bot RESUMED: new entries allowed again.")

    def flatten_requested(self) -> None:
        self._notifier.send("ORB bot FLATTEN: closing every open position this bot owns.")

    def kill_switch(self, detail: str) -> None:
        self._notifier.send(f"ORB bot KILL SWITCH: {detail}")

    def armed(self, mode: str) -> None:
        self._notifier.send(f"ORB bot ARMED for today (mode={mode}).")

    def disarmed(self) -> None:
        self._notifier.send("ORB bot DISARMED.")

    def deployed(self, version: str, sha: str, mode: str) -> None:
        self._notifier.send(f"ORB bot DEPLOYED v{version} ({sha[:8]})\nmode: {mode}")

    def silent(self, age_sec: float, status: str, message: str) -> None:
        self._notifier.send(
            f"ORB bot SILENT: heartbeat age {age_sec:.0f}s, status={status}.\n{message}"
        )

    def order_event(self, text: str) -> None:
        self._notifier.send(text)
