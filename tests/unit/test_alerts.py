"""Alerts: which events reach the operator."""

from __future__ import annotations

from orb_intraday_bot.core.events import LevelSet
from orb_intraday_bot.core.models import Direction, Zone
from orb_intraday_bot.core.signals import PatternEvent, PatternKind, Setup, SetupSignal
from orb_intraday_bot.runtime.alerts import Alerts
from orb_intraday_bot.runtime.day import CandleReport, SignalOutcome
from orb_intraday_bot.runtime.report import _rr
from tests.helpers import at, candle
from tests.runtime_support import Recorder, make_day, real_day


def _reports() -> list[CandleReport]:
    """The real 15 Oct session, one report per candle (an ORB long opens at 09:33)."""
    day = make_day()
    return [day.on_candle(c) for c in real_day()]


def _bar(hh: int = 9, mm: int = 21, close: float = 22750.0) -> CandleReport:
    return CandleReport(
        candle=candle(at(hh, mm), close, close, close, close),
        symbol="NIFTY",
        orh=None,
        orl=None,
        zone=Zone.NEUTRAL,
        pivots=None,
    )


def _signal() -> SetupSignal:
    return SetupSignal(
        setup=Setup.ORB_BREAKOUT,
        direction=Direction.LONG,
        level_name="ORH",
        level_value=22800.0,
        candle_start=at(9, 33),
        entry_price=22810.0,
        entry_basis="test",
        sign_id="sig",
        stop_level=22770.0,
        stop_on="close",
        target_ref="R2",
    )


def test_opened_position_is_alerted() -> None:
    notes = Recorder()
    alerts = Alerts(notes)
    for report in _reports():
        alerts.candle(report)
    assert "OPENED" in notes.joined()
    assert "ORH set" in notes.joined() or "OR complete" in notes.joined()
    assert "SIGN" in notes.joined()


def test_risk_reward_is_reward_over_risk() -> None:
    assert _rr(202.05, 183.30, 212.25) == "1 : 0.54"
    assert _rr(202.05, 183.30, 222.50) == "1 : 1.09"
    assert _rr(100.0, 100.0, 110.0) == "n/a"


def test_opened_telegram_card_has_time_buy_sl_target_and_rr() -> None:
    notes = Recorder()
    alerts = Alerts(notes)
    for report in _reports():
        alerts.candle(report)
    card = next(m for m in notes.messages if "<b>BUY</b>" in m)
    assert "IST" in card and "OPENED" in card
    assert "<b>SL</b>" in card and "<b>T1</b>" in card and "<b>T2</b>" in card
    assert "R:R" in card and "1 :" in card


def test_candle_reports_are_sent_only_when_enabled() -> None:
    reports = _reports()
    quiet, chatty = Recorder(), Recorder()
    for report in reports:
        Alerts(quiet).candle(report)
        Alerts(chatty, every_candle=True).candle(report)
    assert len(chatty.messages) > len(quiet.messages)
    assert len(chatty.messages) >= len(reports)


def test_or_levels_and_completion_are_alerted() -> None:
    notes = Recorder()
    alerts = Alerts(notes)
    first = _bar(9, 18)
    first.orh = 22850.0
    first.level_sets = [LevelSet("ORH", 22850.0, at(9, 18))]
    second = _bar(9, 21)
    second.orh, second.orl = 22850.0, 22680.0
    second.level_sets = [LevelSet("ORL", 22680.0, at(9, 21))]
    both = _bar(9, 24)
    both.orh, both.orl = 22850.0, 22680.0
    both.level_sets = [
        LevelSet("ORH", 22850.0, at(9, 24)),
        LevelSet("ORL", 22680.0, at(9, 24)),
    ]
    alerts.candle(first)
    alerts.candle(second)
    Alerts(notes).candle(both)
    assert notes.messages[0] == "09:18 ORH set at 22850.0 (ORL still forming)"
    assert notes.messages[1] == (
        "09:21 ORL set at 22680.0 — OR complete: ORH 22850.0 / ORL 22680.0"
    )
    assert "09:24 OR complete: ORH 22850.0 / ORL 22680.0" in notes.messages


def test_zone_change_is_alerted_after_the_first_classification() -> None:
    notes = Recorder()
    alerts = Alerts(notes)
    first, second, third = _bar(9, 15), _bar(9, 18, 22950.0), _bar(9, 21, 22950.0)
    first.zone, second.zone, third.zone = Zone.NEUTRAL, Zone.BULLISH, Zone.BULLISH
    alerts.candle(first)
    alerts.candle(second)
    alerts.candle(third)
    assert notes.messages == ["09:18 Zone Neutral → Bullish (close 22950.0)"]


def test_pattern_decisions_are_alerted_and_noise_is_not() -> None:
    notes = Recorder()
    alerts = Alerts(notes)
    report = _bar(9, 33)
    report.events = [
        PatternEvent(PatternKind.SIGN, "ORH", Direction.LONG, at(9, 27), "close 22810"),
        PatternEvent(PatternKind.RETEST, "ORH", Direction.LONG, at(9, 30), "low 22800"),
        PatternEvent(PatternKind.CONFIRMED, "ORH", Direction.LONG, at(9, 33), "broke swing"),
        PatternEvent(
            PatternKind.FAILED_CASE_1, "ORL", Direction.SHORT, at(9, 36), "close back inside"
        ),
        PatternEvent(PatternKind.TRAP_CANCELLED, "ORH", Direction.SHORT, at(9, 39), "beyond again"),
        PatternEvent(PatternKind.FAKE_CANCELLED, "RANGE", Direction.LONG, at(9, 42), "stop"),
        PatternEvent(PatternKind.WHIPSAW_CANCELLED, "ORH", Direction.LONG, at(9, 45), "stop"),
        PatternEvent(PatternKind.CONSOLIDATION_TOUCH, "ORH", Direction.LONG, at(9, 48), "#2"),
        PatternEvent(PatternKind.TREND_DAY, "DAY", Direction.LONG, at(9, 51), "UPTREND: ORH_BO"),
    ]
    alerts.candle(report)
    joined = notes.joined()
    assert "09:27 SIGN ORH LONG: close 22810" in joined
    assert "FAILED_CASE_1 ORL SHORT" in joined
    assert "TRAP_CANCELLED" in joined and "FAKE_CANCELLED" in joined
    assert "WHIPSAW_CANCELLED" in joined
    assert "09:51 TREND_DAY UPTREND: ORH_BO" in joined
    assert "RETEST" not in joined and "CONFIRMED" not in joined
    assert "CONSOLIDATION_TOUCH" not in joined


def test_blocked_signals_are_alerted_opened_is_not_duplicated() -> None:
    notes = Recorder()
    sig = _signal()
    report = _bar(9, 33)
    report.outcomes = [
        SignalOutcome(sig, "rejected", "above R1: no shorts", "zone"),
        SignalOutcome(sig, "skipped", "already rejected earlier"),
    ]
    Alerts(notes).candle(report)
    assert notes.messages == [
        "09:33 BLOCKED ORB_BREAKOUT ORH LONG @ 22810.0 [zone] above R1: no shorts"
    ]


def test_catch_up_skips_decision_alerts_but_keeps_missed() -> None:
    notes = Recorder()
    alerts = Alerts(notes)
    report = _bar(9, 21)
    report.catch_up = True
    report.orh = 22850.0
    report.level_sets = [LevelSet("ORH", 22850.0, at(9, 21))]
    report.events = [PatternEvent(PatternKind.SIGN, "ORH", Direction.LONG, at(9, 21), "close")]
    report.outcomes = [SignalOutcome(_signal(), "stale", "too old", "stale")]
    alerts.candle(report)
    assert all("ORH set" not in m and "SIGN" not in m for m in notes.messages)
    assert "MISSED" in notes.joined()
    live = _bar(9, 24, 22950.0)
    live.zone = Zone.BULLISH
    alerts.candle(live)
    assert notes.messages[-1] == "09:24 Zone Neutral → Bullish (close 22950.0)"


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
    alerts.deployed("0.9.0", "deadbeef", "paper")
    alerts.silent(180, "running", "no heartbeat")
    alerts.shutdown("idle")
    joined = notes.joined()
    for word in (
        "STARTED", "banner", "PAUSED", "RESUMED", "FLATTEN", "KILL SWITCH",
        "DEPLOYED", "SILENT", "STOPPED",
    ):
        assert word in joined
