"""Phase 8: trading day, live runner, watchdog, heartbeat, alerts, CLI.

The live runner is driven by a fake data source and a fixed clock whose `sleep` advances time, so
a whole session runs in milliseconds. The day used is the real 15 Oct 2026 morning (an ORB long
confirms at 09:33).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from orb_intraday_bot import cli
from orb_intraday_bot.core.clock import IST
from orb_intraday_bot.core.signals import Setup
from orb_intraday_bot.options.planner import OptionPlanner
from orb_intraday_bot.paper.models import ExitReason
from orb_intraday_bot.runtime.day import TradingDay
from orb_intraday_bot.runtime.monitor import (
    Watchdog,
    another_instance_running,
    read_heartbeat,
    write_heartbeat,
)
from orb_intraday_bot.runtime.notify import NullNotifier, TelegramNotifier
from orb_intraday_bot.runtime.report import format_candle_report
from orb_intraday_bot.storage.store import TradeStore
from tests.helpers import at
from tests.runtime_support import (
    CFG,
    DAY,
    LOT,
    Harness,
    NoChain,
    Recorder,
    make_day,
    real_day,
)


# ---- TradingDay --------------------------------------------------------------------------
def test_real_day_opens_the_orb_long_at_0933() -> None:
    store = TradeStore(":memory:")
    day = make_day(store)
    opened_at = []
    for candle in real_day():
        report = day.on_candle(candle)
        opened_at += [(candle.start.strftime("%H:%M"), o) for o in report.opened]
    assert opened_at[0][0] == "09:33"
    first = opened_at[0][1]
    assert first.signal.setup is Setup.ORB_BREAKOUT
    assert first.position is not None and first.position.lots >= 1
    assert len(store.positions_for_day(DAY)) == len(opened_at)
    assert store.day_summary(DAY).trades == len(opened_at)


def test_catch_up_replay_is_idempotent() -> None:
    store = TradeStore(":memory:")
    first = make_day(store)
    for candle in real_day():
        first.on_candle(candle)
    before = store.day_summary(DAY)
    count = len(store.positions_for_day(DAY))

    restarted = make_day(store)             # same database, a fresh process
    for candle in real_day():
        report = restarted.on_candle(candle, catch_up=True)
        assert report.opened == []
    assert len(store.positions_for_day(DAY)) == count
    after = store.day_summary(DAY)
    assert (after.trades, after.net_option_pnl) == (before.trades, before.net_option_pnl)


def test_stale_signal_is_never_opened() -> None:
    store = TradeStore(":memory:")
    day = make_day(store)
    outcomes = []
    for candle in real_day():
        outcomes += day.on_candle(candle, allow_open=False, catch_up=True).outcomes
    assert any(o.status == "stale" for o in outcomes)
    assert store.positions_for_day(DAY) == []
    rows = store.signals_for_day(DAY)
    assert any(r["status"] == "rejected" and r["stage"] == "stale" for r in rows)


def test_option_failure_rejects_and_releases_the_signal() -> None:
    store = TradeStore(":memory:")
    day = make_day(store, OptionPlanner(CFG, NoChain(LOT)))
    outcomes = []
    for candle in real_day():
        outcomes += day.on_candle(candle).outcomes
    failed = [o for o in outcomes if o.status == "rejected" and o.stage == "option"]
    assert failed
    assert store.positions_for_day(DAY) == []
    assert any(r["status"] == "released" for r in store.signals_for_day(DAY))


def test_candle_report_has_the_levels_and_the_trade() -> None:
    day = make_day()
    texts = [format_candle_report(day.on_candle(c)) for c in real_day()]
    assert "NIFTY26OCTFUT" in texts[0]
    assert "OPENED" in "\n".join(texts[:8])
    assert "ORH" in texts[3]


# ---- live runner (fakes live in tests/runtime_support.py) --------------------------------
def test_full_session_from_before_the_open(tmp_path: Path) -> None:
    h = Harness(tmp_path, at(9, 10))
    outcome = h.runner().run()
    assert outcome.exit_code == 0
    assert "Waiting for the market" in h.text
    assert "OPENED" in h.text
    assert "OPENED" in h.notes.joined()
    # candles stop at 10:03. Neutral-zone targets often close the morning trades on the
    # next bar; if anything is still open the watchdog force-closes at 15:35.
    positions = h.store.positions_for_day(DAY)
    assert len(positions) >= 1
    assert all(p.status.value == "CLOSED" for p in positions)
    if any(p.closed_at and p.closed_at.hour >= 15 for p in positions):
        assert "no new candle" in h.notes.joined()
        assert "last candle never arrived" in h.text
        assert any(f.reason is ExitReason.EOD for p in positions for f in p.fills)
    assert "charges" in h.text and "net" in h.text.splitlines()[-1]       # the day summary
    info = read_heartbeat(tmp_path / CFG.runtime.heartbeat_file)
    assert info is not None and info.status == "finished"


def test_late_start_catches_up_and_misses_the_old_signal(tmp_path: Path) -> None:
    h = Harness(tmp_path, at(10, 3, DAY) + timedelta(seconds=5))
    h.runner().run()
    assert "MISSED" in h.notes.joined()
    assert "OPENED" not in h.notes.joined()
    assert h.store.positions_for_day(DAY) == []
    assert "09:33" in h.text          # the old candles were still processed and reported


def test_data_errors_are_retried_and_alerted(tmp_path: Path) -> None:
    h = Harness(tmp_path, at(9, 10))
    h.source.fail_fetches = 4
    outcome = h.runner().run()
    assert outcome.exit_code == 0
    assert "market data failing" in h.notes.joined()
    assert "OPENED" in h.text           # it recovered and traded normally


def test_holiday_exits_cleanly(tmp_path: Path) -> None:
    h = Harness(tmp_path, at(9, 0), candles=[])
    outcome = h.runner().run()
    assert outcome.exit_code == 0
    assert "holiday" in outcome.message.lower()


def test_open_position_is_exited_by_a_live_price_poll(tmp_path: Path) -> None:
    h = Harness(tmp_path, at(9, 10))
    h.source.price_override = (at(10, 10), 22700.0)      # far below the stop, after the data ends
    h.runner().run()
    positions = h.store.positions_for_day(DAY)
    assert positions and all(p.status.value == "CLOSED" for p in positions)
    reasons = {f.reason for p in positions for f in p.fills}
    assert reasons & {ExitReason.HARD_STOP, ExitReason.OPTION_STOP}
    assert "<b>SL</b>" in h.notes.joined()
    assert "last candle never arrived" not in h.text      # nothing left to force-close


def test_second_instance_is_refused(tmp_path: Path) -> None:
    h = Harness(tmp_path, at(9, 10))
    beat = {
        "status": "running", "time": h.clock.now().isoformat(), "last_candle": None,
        "open_positions": 0, "errors": 0, "message": "", "pid": os.getpid() + 1,
    }
    (tmp_path / CFG.runtime.heartbeat_file).write_text(json.dumps(beat), encoding="utf-8")
    outcome = h.runner().run()
    assert outcome.exit_code == 3
    assert h.sleeps == 0


def test_strategy_error_stops_the_run_and_alerts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path, at(9, 10))

    def broken(self: TradingDay, *args: object, **kwargs: object) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(TradingDay, "on_candle", broken)
    outcome = h.runner().run()
    assert outcome.exit_code == 1
    assert "STOPPED" in h.notes.joined()
    info = read_heartbeat(tmp_path / CFG.runtime.heartbeat_file)
    assert info is not None and info.status == "error"


# ---- heartbeat and watchdog --------------------------------------------------------------
def test_heartbeat_round_trip_and_instance_check(tmp_path: Path) -> None:
    path = tmp_path / "hb.json"
    now = at(9, 30)
    assert read_heartbeat(path) is None
    write_heartbeat(
        path, now=now, status="running", last_candle=at(9, 27), open_positions=1, errors=2,
        message="hi",
    )
    info = read_heartbeat(path)
    assert info is not None
    assert (info.status, info.open_positions, info.errors) == ("running", 1, 2)
    assert info.last_candle == at(9, 27).isoformat()
    # written by this very process: not "another" instance
    assert not another_instance_running(path, now)


def test_stale_or_finished_heartbeat_does_not_block_a_start(tmp_path: Path) -> None:
    path = tmp_path / "hb.json"
    now = at(9, 30)

    def beat(status: str, age_sec: int) -> None:
        raw = {
            "status": status, "time": (now - timedelta(seconds=age_sec)).isoformat(),
            "last_candle": None, "open_positions": 0, "errors": 0, "message": "",
            "pid": os.getpid() + 1,
        }
        path.write_text(json.dumps(raw), encoding="utf-8")

    beat("running", 10)
    assert another_instance_running(path, now)
    beat("running", 600)
    assert not another_instance_running(path, now)
    beat("finished", 5)
    assert not another_instance_running(path, now)
    path.write_text("not json", encoding="utf-8")
    assert not another_instance_running(path, now)


def test_watchdog_alerts_once_per_episode() -> None:
    notes = Recorder()
    dog = Watchdog(notes, stall_alert_sec=180)
    due = at(9, 30)
    assert not dog.check_late(due + timedelta(seconds=60), due, None)
    assert dog.check_late(due + timedelta(seconds=200), due, at(9, 27))
    assert not dog.check_late(due + timedelta(seconds=400), due, at(9, 27))
    dog.candle_arrived()
    assert dog.check_late(due + timedelta(seconds=300), due, at(9, 30))
    assert len(notes.messages) == 2


def test_watchdog_alerts_after_repeated_errors_only_once() -> None:
    notes = Recorder()
    dog = Watchdog(notes, stall_alert_sec=180)
    for _ in range(6):
        dog.record_error(ConnectionError("x"))
    assert len(notes.messages) == 1
    dog.record_success()
    for _ in range(3):
        dog.record_error(ConnectionError("x"))
    assert len(notes.messages) == 2
    assert dog.errors == 9


# ---- alerts ------------------------------------------------------------------------------
def test_telegram_posts_the_message_and_hides_the_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sent: list[tuple[str, bytes]] = []

    def capture(url: str, body: bytes) -> bytes:
        sent.append((url, body))
        return b'{"ok":true}'

    TelegramNotifier(
        "SECRET-TOKEN", "42", post=capture, async_outbox=False
    ).send("hello")
    assert "SECRET-TOKEN" in sent[0][0]
    assert json.loads(sent[0][1]) == {"chat_id": "42", "text": "hello"}

    def failing(url: str, body: bytes) -> bytes:
        raise OSError(f"cannot reach {url}")

    with caplog.at_level("WARNING", logger="orb.notify"):
        TelegramNotifier(
            "SECRET-TOKEN", "42", post=failing, sleep=lambda _: None, async_outbox=False
        ).send("hello")      # must not raise
    assert "SECRET-TOKEN" not in caplog.text
    assert "OSError" in caplog.text


def test_telegram_truncates_long_messages() -> None:
    sent: list[bytes] = []

    def capture(url: str, body: bytes) -> bytes:
        sent.append(body)
        return b'{"ok":true}'

    TelegramNotifier("t", "1", post=capture, async_outbox=False).send("x" * 10_000)
    assert len(json.loads(sent[0])["text"]) < 4096


def test_null_notifier_is_silent() -> None:
    NullNotifier().send("anything")


# ---- CLI ---------------------------------------------------------------------------------
def test_parser_commands() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["replay", "2026-10-14", "2026-10-15", "--quiet"])
    assert args.dates == ["2026-10-14", "2026-10-15"] and args.events
    assert parser.parse_args(["summary"]).date is None
    assert parser.parse_args(["check", "--send-test-alert"]).send_test_alert
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_status_and_summary_commands(
    tmp_path: Path, cli_context: Callable[..., object], capsys: pytest.CaptureFixture[str]
) -> None:
    cli_context()
    assert cli.main(["status"]) == 1
    assert "not run" in capsys.readouterr().out

    write_heartbeat(
        tmp_path / CFG.runtime.heartbeat_file, now=datetime.now(IST), status="finished",
        last_candle=None, open_positions=0, errors=0,
    )
    assert cli.main(["status"]) == 0
    assert "finished" in capsys.readouterr().out

    assert cli.main(["summary", "2026-10-15"]) == 1          # no database yet
    store = TradeStore(tmp_path / CFG.paper.database_file)
    day = make_day(store)
    for candle in real_day():
        day.on_candle(candle)
    store.close()
    assert cli.main(["summary", "2026-10-15"]) == 0
    assert "1" in capsys.readouterr().out
