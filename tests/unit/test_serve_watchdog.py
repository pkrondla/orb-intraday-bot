"""Serve loop and watchdog: idle sleep, stop file, dead-man alert."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from pydantic import SecretStr

from orb_intraday_bot.app.context import AppContext
from orb_intraday_bot.app.serve import ServeState, serve_loop, should_stop, stop_file_path
from orb_intraday_bot.app.watchdog import check_once, in_market_hours
from orb_intraday_bot.config.settings import Settings, StrategyConfig
from orb_intraday_bot.core.clock import IST, FixedClock
from orb_intraday_bot.risk.engine import RiskEngine
from orb_intraday_bot.runtime.alerts import Alerts
from orb_intraday_bot.runtime.live import RunOutcome
from orb_intraday_bot.runtime.monitor import write_heartbeat
from orb_intraday_bot.runtime.telegram_commands import CommandBus
from tests.runtime_support import Recorder


def _ctx(tmp_path: Path) -> AppContext:
    settings = Settings.model_construct(
        data_dir=tmp_path,
        telegram_bot_token=SecretStr(""),
        telegram_chat_id="",
        healthcheck_url="",
        live_mode="paper",
        live_armed=False,
        strategy_config_path=Path("config/default.yaml"),
    )
    return AppContext(settings, StrategyConfig(), tmp_path)


def test_stop_file_halts_serve(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    state = ServeState(bus=CommandBus(), risk=RiskEngine())
    assert not should_stop(ctx.data_dir, state)
    stop_file_path(ctx.data_dir).write_text("x", encoding="utf-8")
    assert should_stop(ctx.data_dir, state)


def test_serve_loop_runs_one_session_and_stops(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    notes = Recorder()
    clock = FixedClock(datetime(2026, 10, 16, 9, 0, tzinfo=IST))  # Friday session start
    calls: list[date] = []

    def session(c, n, s, cl, sl):  # noqa: ANN001
        calls.append(cl.now().date())
        return RunOutcome(0, "ok")

    sleeps: list[float] = []

    def sleep(sec: float) -> None:
        sleeps.append(sec)
        # After the session, jump past any further waits by requesting stop.
        stop_file_path(tmp_path).write_text("stop\n", encoding="utf-8")

    code = serve_loop(
        ctx,
        clock=clock,
        sleep=sleep,
        notifier=notes,
        session=session,
        max_sessions=1,
        idle_step_sec=1,
        holidays=frozenset(),
    )
    assert code == 0
    assert calls == [date(2026, 10, 16)]
    assert any("STARTED" in m for m in notes.messages)
    assert any("STOPPED" in m for m in notes.messages)


def test_in_market_hours() -> None:
    assert in_market_hours(datetime(2026, 10, 16, 10, 0, tzinfo=IST))
    assert not in_market_hours(datetime(2026, 10, 16, 8, 0, tzinfo=IST))
    assert not in_market_hours(datetime(2026, 10, 16, 16, 0, tzinfo=IST))


def test_watchdog_alerts_on_stale_heartbeat(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    notes = Recorder()
    alerts = Alerts(notes)
    now = datetime(2026, 10, 16, 11, 0, tzinfo=IST)
    write_heartbeat(
        tmp_path / "heartbeat.json",
        now=now - timedelta(seconds=200),
        status="running",
        last_candle=None,
        open_positions=0,
        errors=0,
        message="stuck",
    )
    alerted = check_once(
        ctx, alerts, now, holidays=frozenset(), ping=lambda _: None, last_alerted_at=None
    )
    assert alerted == now
    assert any("SILENT" in m for m in notes.messages)


def test_watchdog_ping_when_healthy(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.settings.healthcheck_url = "https://example.test/ping"  # type: ignore[misc]
    # Settings is frozen dataclass-like via pydantic - AppContext has settings as field
    # Rebuild with healthcheck
    settings = Settings.model_construct(
        data_dir=tmp_path,
        telegram_bot_token=SecretStr(""),
        telegram_chat_id="",
        healthcheck_url="https://example.test/ping",
        live_mode="paper",
        live_armed=False,
        strategy_config_path=Path("config/default.yaml"),
    )
    ctx = AppContext(settings, StrategyConfig(), tmp_path)
    notes = Recorder()
    alerts = Alerts(notes)
    now = datetime(2026, 10, 16, 11, 0, tzinfo=IST)
    write_heartbeat(
        tmp_path / "heartbeat.json",
        now=now,
        status="running",
        last_candle=None,
        open_positions=0,
        errors=0,
    )
    pings: list[str] = []
    check_once(ctx, alerts, now, holidays=frozenset(), ping=pings.append, last_alerted_at=None)
    assert pings == ["https://example.test/ping"]
    assert notes.messages == []
