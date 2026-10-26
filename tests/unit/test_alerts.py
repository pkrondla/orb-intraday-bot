"""Alerts: which events reach the operator."""

from __future__ import annotations

from orb_intraday_bot.core.events import LevelSet
from orb_intraday_bot.core.models import Direction, Zone
from orb_intraday_bot.core.signals import PatternEvent, PatternKind, Setup, SetupSignal
from orb_intraday_bot.paper.exits import ExitEvent
from orb_intraday_bot.paper.models import ExitReason, Fill
from orb_intraday_bot.runtime.alerts import Alerts
from orb_intraday_bot.runtime.day import CandleReport, SignalOutcome
from orb_intraday_bot.engine.signal_engine import PlannedTrade
from orb_intraday_bot.risk.stops import StopPlan
from orb_intraday_bot.risk.targets import TargetPlan
from orb_intraday_bot.runtime.report import _rr, sl_level_name, target_level_name
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


def _planned(
    stop_basis: str,
    *,
    level_name: str = "ORL",
    t1_basis: str = "nearest pivot P",
    t2_basis: str = "S2",
) -> PlannedTrade:
    return PlannedTrade(
        _signal() if level_name == "ORH" else SetupSignal(
            setup=Setup.TRAP_REVERSE_RETEST,
            direction=Direction.LONG,
            level_name=level_name,
            level_value=22750.0,
            candle_start=at(9, 42),
            entry_price=22780.0,
            entry_basis="test",
            sign_id="trap",
            stop_level=22750.0,
            stop_on="touch",
            target_ref="ORH",
        ),
        Zone.NEUTRAL,
        StopPlan(35.0, 22745.0, None, stop_basis),
        TargetPlan(22790.0, 22850.0, 10.0, 70.0, t1_basis, t2_basis),
        5_000.0,
    )


def test_opened_card_labels_sl_t1_t2_levels() -> None:
    assert sl_level_name(_planned("ORH is 20.0 pts away", level_name="ORH")) == "ORH"
    assert sl_level_name(_planned("level is 18.5 pts away", level_name="ORL")) == "ORL"
    assert sl_level_name(_planned("level is 42.0 pts away: hard stop 35")) == "35 pts"
    assert sl_level_name(_planned("standard 35-point stop")) == "35 pts"
    assert sl_level_name(_planned("level is 8.0 pts away: raised to the 10-point floor")) == "10 pts"
    assert sl_level_name(_planned("level is 18.5 pts away", level_name="2HH")) == "2HH"
    assert target_level_name("nearest pivot P") == "P"
    assert target_level_name("nearest pivot S1") == "S1"
    assert target_level_name("P (neutral zone, nearest pivot)") == "P"
    assert target_level_name("ORH") == "ORH"
    assert target_level_name("halfway to T2") == "halfway"
    assert target_level_name("S2") == "S2"
    assert target_level_name("ORL, capped at S2") == "S2"
    assert target_level_name("S4 (entry already beyond S2)") == "S4"
    assert target_level_name("nearer of projection and pivot: pivot S1") == "S1"


def test_opened_telegram_card_has_time_buy_sl_target_and_rr() -> None:
    notes = Recorder()
    alerts = Alerts(notes)
    opened: SignalOutcome | None = None
    for report in _reports():
        alerts.candle(report)
        if opened is None and report.opened:
            opened = report.opened[0]
    assert opened is not None and opened.position is not None
    card = next(m for m in notes.messages if "<b>BUY</b>" in m)
    pos = opened.position
    assert "IST" in card and "OPENED" in card
    assert "<b>SL</b>" in card and "<b>T1</b>" in card and "<b>T2</b>" in card
    assert "R:R" in card and "1 :" in card
    buy_block = card.split("<b>SL</b>", 1)[0]
    assert f"fut <code>{pos.futures_entry:.1f}</code>" in buy_block
    assert f"fut <code>{pos.hard_stop:.1f}</code>" in card
    assert f"fut <code>{pos.t1:.1f}</code>" in card
    assert f"fut <code>{pos.t2:.1f}</code>" in card
    if abs(pos.t2_hit_level - pos.t2) >= 1e-6:
        assert f"trigger <code>{pos.t2_hit_level:.1f}</code>" in card
    trade = opened.trade
    assert trade is not None
    assert sl_level_name(trade) in card
    assert target_level_name(trade.targets.t1_basis) in card
    assert target_level_name(trade.targets.t2_basis) in card


def _exit_fill(reason: ExitReason, futures: float, option: float, points: float) -> Fill:
    return Fill(
        time=at(10, 15),
        reason=reason,
        lots=1,
        quantity=75,
        futures_price=futures,
        option_price=option,
        price_source="live",
        option_gross=round((option - 150.0) * 75, 2),
        futures_points=points,
        charges=25.0,
    )


def test_exit_telegram_card_has_futures_on_sl_t1_t2() -> None:
    notes = Recorder()
    alerts = Alerts(notes)
    alerts.exit(ExitEvent(
        "p1", at(10, 15),
        "NIFTY26O1922800CE HARD_STOP: sold 1 lot(s) at 130.75 (live), futures 22765.0",
        _exit_fill(ExitReason.HARD_STOP, 22765.0, 130.75, -35.0),
    ))
    alerts.exit(ExitEvent(
        "p1", at(10, 21),
        "NIFTY26O1922800CE T1: sold 1 lot(s) at 166.50 (live), futures 22830.0",
        _exit_fill(ExitReason.T1, 22830.0, 166.50, 30.0),
    ))
    alerts.exit(ExitEvent(
        "p1", at(10, 21),
        "NIFTY26O1922800CE T1 reached: stop moved to entry (futures 22800.0, option 150.00)",
    ))
    alerts.exit(ExitEvent(
        "p1", at(10, 45),
        "NIFTY26O1922800CE T2: sold 1 lot(s) at 205.00 (live), futures 22900.0",
        _exit_fill(ExitReason.T2, 22900.0, 205.00, 100.0),
    ))
    sl, t1, moved, t2 = notes.messages
    assert "<b>SL</b>" in sl and "HARD_STOP" in sl
    assert "fut <code>22765.0</code>" in sl and "option <code>130.75</code>" in sl
    assert "<b>T1</b>" in t1 and "fut <code>22830.0</code>" in t1
    assert "<b>T1</b>" in moved and "stop moved to entry" in moved
    assert "fut <code>22800.0</code>" in moved and "option <code>150.00</code>" in moved
    assert "<b>T2</b>" in t2 and "fut <code>22900.0</code>" in t2


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
