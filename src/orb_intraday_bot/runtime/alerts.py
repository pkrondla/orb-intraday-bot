"""What the bot tells the operator, and when. The runner decides nothing about wording.

All messages go through one object so the rules (which events alert, what they say) live in one
place and a second channel is a change here only.

A quiet candle is not sent. Decision alerts (opening-range levels, a zone change, a new SIGN,
a failed or cancelled pattern, a blocked signal) are. OPENED and SL / T1 / T2 exits are HTML
cards; other exits use the same card with the reason as the heading.
`notify_candle_reports` still adds the full per-candle report on top.
"""

from __future__ import annotations

from orb_intraday_bot.core.models import Zone
from orb_intraday_bot.core.signals import PatternKind
from orb_intraday_bot.paper.exits import ExitEvent
from orb_intraday_bot.runtime.day import CandleReport, SignalOutcome
from orb_intraday_bot.runtime.notify import Notifier, as_telegram_html
from orb_intraday_bot.runtime.report import (
    format_candle_report,
    format_exit_alert,
    format_opened_alert,
    format_outcome,
)

# CONFIRMED is omitted: the OPENED or BLOCKED line for that candle already says the signal fired.
_PATTERN_ALERTS = frozenset({
    PatternKind.SIGN,
    PatternKind.FAILED_CASE_1,
    PatternKind.FAILED_CASE_2,
    PatternKind.TRAP_CANCELLED,
    PatternKind.FAKE_CANCELLED,
    PatternKind.WHIPSAW_CANCELLED,
    PatternKind.TREND_DAY,
})


def _zone_label(zone: Zone) -> str:
    return zone.value.capitalize()


class Alerts:
    def __init__(self, notifier: Notifier, every_candle: bool = False) -> None:
        self._notifier = notifier
        self._every_candle = every_candle
        self._last_zone: Zone | None = None

    def candle(self, report: CandleReport) -> None:
        """Decision alerts on a live candle; opened / missed / exits always; full report if on."""
        if report.catch_up:
            self._last_zone = report.zone
        else:
            self._level_alerts(report)
            self._zone_alert(report)
            self._pattern_alerts(report)
            self._blocked_alerts(report)
        for outcome in report.opened:
            self._notifier.send(as_telegram_html(format_opened_alert(outcome)))
        for outcome in report.outcomes:
            if outcome.status == "stale":
                self._notifier.send(self._missed(outcome))
        for event in report.exits:
            self.exit(event)
        if self._every_candle:
            self._notifier.send(format_candle_report(report))

    def _level_alerts(self, report: CandleReport) -> None:
        if not report.level_sets:
            return
        stamp = report.candle.start.strftime("%H:%M")
        complete = report.orh is not None and report.orl is not None
        kinds = {lv.kind for lv in report.level_sets}
        if complete and kinds >= {"ORH", "ORL"}:
            self._notifier.send(
                f"{stamp} OR complete: ORH {report.orh:.1f} / ORL {report.orl:.1f}"
            )
            return
        lv = report.level_sets[0]
        if complete:
            self._notifier.send(
                f"{stamp} {lv.kind} set at {lv.value:.1f} — OR complete: "
                f"ORH {report.orh:.1f} / ORL {report.orl:.1f}"
            )
            return
        missing = "ORL" if lv.kind == "ORH" else "ORH"
        self._notifier.send(
            f"{stamp} {lv.kind} set at {lv.value:.1f} ({missing} still forming)"
        )

    def _zone_alert(self, report: CandleReport) -> None:
        zone = report.zone
        if self._last_zone is None or zone is self._last_zone:
            self._last_zone = zone
            return
        stamp = report.candle.start.strftime("%H:%M")
        self._notifier.send(
            f"{stamp} Zone {_zone_label(self._last_zone)} → {_zone_label(zone)} "
            f"(close {report.candle.close:.1f})"
        )
        self._last_zone = zone

    def _pattern_alerts(self, report: CandleReport) -> None:
        for event in report.events:
            if event.kind not in _PATTERN_ALERTS:
                continue
            stamp = event.candle_start.strftime("%H:%M")
            if event.kind is PatternKind.TREND_DAY:
                self._notifier.send(f"{stamp} TREND_DAY {event.detail}")
                continue
            extra = f": {event.detail}" if event.detail else ""
            self._notifier.send(
                f"{stamp} {event.kind.value} {event.level_name} {event.direction.value}{extra}"
            )

    def _blocked_alerts(self, report: CandleReport) -> None:
        for outcome in report.outcomes:
            if outcome.status == "rejected":
                stamp = outcome.signal.candle_start.strftime("%H:%M")
                self._notifier.send(f"{stamp} {format_outcome(outcome)}")

    @staticmethod
    def _missed(outcome: SignalOutcome) -> str:
        s = outcome.signal
        return (
            f"MISSED {s.setup.value} {s.level_name} {s.direction.value} @ "
            f"{s.entry_price:.1f}: the bot was not running when it fired."
        )

    def exit(self, event: ExitEvent) -> None:
        self._notifier.send(as_telegram_html(format_exit_alert(event)))

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

    def paused(self, day: str = "today") -> None:
        self._notifier.send(
            f"ORB bot PAUSED for {day}: no new entries. Open positions still managed."
        )

    def resumed(self, day: str = "today") -> None:
        self._notifier.send(f"ORB bot RESUMED for {day}: new entries allowed again.")

    def flatten_requested(self) -> None:
        self._notifier.send("ORB bot FLATTEN: closing every open position this bot owns.")

    def kill_switch(self, detail: str) -> None:
        self._notifier.send(f"ORB bot KILL SWITCH: {detail}")

    def deployed(self, version: str, sha: str, mode: str) -> None:
        self._notifier.send(f"ORB bot DEPLOYED v{version} ({sha[:8]})\nmode: {mode}")

    def silent(self, age_sec: float, status: str, message: str) -> None:
        self._notifier.send(
            f"ORB bot SILENT: heartbeat age {age_sec:.0f}s, status={status}.\n{message}"
        )

    def order_event(self, text: str) -> None:
        self._notifier.send(text)
