"""Alerts: which events reach the operator."""

from __future__ import annotations

from orb_intraday_bot.runtime.alerts import Alerts
from orb_intraday_bot.runtime.day import CandleReport
from tests.runtime_support import Recorder, make_day, real_day


def _reports() -> list[CandleReport]:
    """The real 15 Oct session, one report per candle (an ORB long opens at 09:33)."""
    day = make_day()
    return [day.on_candle(c) for c in real_day()]


def test_opened_position_is_alerted() -> None:
    notes = Recorder()
    alerts = Alerts(notes)
    for report in _reports():
        alerts.candle(report)
    assert "OPENED" in notes.joined()


def test_candle_reports_are_sent_only_when_enabled() -> None:
    reports = _reports()
    quiet, chatty = Recorder(), Recorder()
    for report in reports:
        Alerts(quiet).candle(report)
        Alerts(chatty, every_candle=True).candle(report)
    assert len(chatty.messages) > len(quiet.messages)
    assert len(chatty.messages) >= len(reports)


def test_day_end_and_error_messages() -> None:
    notes = Recorder()
    alerts = Alerts(notes)
    alerts.day_end("summary text")
    alerts.stopped_with_error("RuntimeError: bug")
    assert notes.messages[0] == "summary text"
    assert "STOPPED" in notes.messages[1] and "Positions are saved" in notes.messages[1]


def test_lifecycle_alerts() -> None:
    notes = Recorder()
    alerts = Alerts(notes)
    alerts.startup("0.9.0", "paper", "abc123")
    alerts.session_start("banner")
    alerts.paused()
    alerts.resumed()
    alerts.flatten_requested()
    alerts.kill_switch("daily loss")
    alerts.armed("live")
    alerts.disarmed()
    alerts.deployed("0.9.0", "deadbeef", "paper")
    alerts.silent(180, "running", "no heartbeat")
    alerts.shutdown("idle")
    joined = notes.joined()
    for word in (
        "STARTED", "banner", "PAUSED", "RESUMED", "FLATTEN", "KILL SWITCH",
        "ARMED", "DISARMED", "DEPLOYED", "SILENT", "STOPPED",
    ):
        assert word in joined
