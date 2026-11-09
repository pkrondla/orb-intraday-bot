"""/holidays and /holiday remove: show the calendar and open a date the file marks closed."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from orb_intraday_bot.app.serve import serve_loop, stop_file_path
from orb_intraday_bot.app.watchdog import check_once
from orb_intraday_bot.core.clock import IST, FixedClock
from orb_intraday_bot.runtime.alerts import Alerts
from orb_intraday_bot.runtime.holiday_overrides import (
    OVERRIDE_FILE_NAME,
    load_removed,
    save_removed,
)
from orb_intraday_bot.runtime.live import RunOutcome
from orb_intraday_bot.runtime.monitor import write_heartbeat
from orb_intraday_bot.runtime.telegram_commands import CommandKind, parse_command
from tests.runtime_support import Recorder
from tests.unit.test_operator_commands import _send, _state
from tests.unit.test_serve_execution import _ctx
from tests.unit.test_serve_watchdog import _ctx as _watch_ctx

NOW = datetime(2026, 11, 9, 10, 0, tzinfo=IST)   # Monday, a normal session
BALIPRATIPADA = date(2026, 11, 10)


def test_holiday_commands_are_parsed() -> None:
    listed = parse_command("/holidays")
    assert (listed.kind, listed.arg) == (CommandKind.HOLIDAYS, "")
    remove = parse_command("/holiday remove 2026-11-10")
    assert (remove.kind, remove.arg) == (CommandKind.HOLIDAY, "remove 2026-11-10")
    alias = parse_command("/holidays remove 2026-11-10")
    assert alias.kind is CommandKind.HOLIDAY


def test_holidays_lists_the_file_and_a_removal(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    state = _state()
    listed = _send(CommandKind.HOLIDAYS, "", ctx=ctx, state=state, now=NOW)[0]
    assert "Today 2026-11-09 Monday: trading day" in listed
    assert "Next session: now" in listed
    assert "2026-11-10 Tue" in listed and "Balipratipada" in listed
    assert "Removed by Telegram" not in listed

    done = _send(CommandKind.HOLIDAY, "remove 2026-11-10", ctx=ctx, state=state, now=NOW)[0]
    assert "Removed 2026-11-10" in done
    assert "session starts on the next check" in done
    assert load_removed(tmp_path) == {BALIPRATIPADA}

    again = _send(CommandKind.HOLIDAYS, "", ctx=ctx, state=state, now=NOW)[0]
    assert "2026-11-10 Tue" not in again.split("Removed by Telegram")[0]
    assert "Removed by Telegram" in again and "2026-11-10" in again
    assert (tmp_path / OVERRIDE_FILE_NAME).exists()


def test_remove_refuses_a_weekend_a_missing_date_and_a_repeat(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    state = _state()
    weekend = _send(CommandKind.HOLIDAY, "remove 2026-11-08", ctx=ctx, state=state, now=NOW)[0]
    assert "weekend" in weekend and load_removed(tmp_path) == set()
    missing = _send(CommandKind.HOLIDAY, "remove 2026-11-12", ctx=ctx, state=state, now=NOW)[0]
    assert "not in the holiday list" in missing
    assert "Usage" in _send(CommandKind.HOLIDAY, "", ctx=ctx, state=state, now=NOW)[0]
    assert "YYYY-MM-DD" in _send(CommandKind.HOLIDAY, "remove 09-11-2026", ctx=ctx, state=state)[0]
    _send(CommandKind.HOLIDAY, "remove 2026-11-10", ctx=ctx, state=state, now=NOW)
    second = _send(CommandKind.HOLIDAY, "remove 2026-11-10", ctx=ctx, state=state, now=NOW)[0]
    assert "already removed" in second


def test_removing_todays_holiday_starts_the_waiting_session(tmp_path: Path) -> None:
    ctx = _watch_ctx(tmp_path)
    notes = Recorder()
    day = date(2026, 11, 9)
    clock = FixedClock(datetime(2026, 11, 9, 10, 0, tzinfo=IST))
    calls: list[date] = []
    wrote = False

    def session(c, n, s, cl, sl):  # noqa: ANN001
        calls.append(cl.now().date())
        return RunOutcome(0, "ok")

    def sleep(sec: float) -> None:
        nonlocal wrote
        assert sec > 0
        if not wrote:
            save_removed(tmp_path, {day})
            wrote = True
            return
        stop_file_path(tmp_path).write_text("stop\n", encoding="utf-8")

    code = serve_loop(
        ctx,
        clock=clock,
        sleep=sleep,
        notifier=notes,
        session=session,
        max_sessions=1,
        idle_step_sec=15,
        holidays=frozenset({day}),
    )
    assert code == 0
    assert calls == [day]
    assert wrote


def test_watchdog_alerts_once_a_holiday_is_removed(tmp_path: Path) -> None:
    ctx = _watch_ctx(tmp_path)
    notes = Recorder()
    now = datetime(2026, 10, 2, 11, 0, tzinfo=IST)   # Gandhi Jayanti in the holiday file
    write_heartbeat(
        tmp_path / "heartbeat.json",
        now=now,
        status="finished",
        last_candle=None,
        open_positions=0,
        errors=0,
        message="session complete",
    )
    quiet = check_once(ctx, Alerts(notes), now, ping=lambda _: None, last_alerted_at=None)
    assert quiet is None and notes.messages == []
    save_removed(tmp_path, {date(2026, 10, 2)})
    alerted = check_once(ctx, Alerts(notes), now, ping=lambda _: None, last_alerted_at=None)
    assert alerted == now
    assert any("SILENT" in message for message in notes.messages)
