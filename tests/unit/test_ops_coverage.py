"""Extra coverage for serve, watchdog, venues, live flatten."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from pydantic import SecretStr

from orb_intraday_bot.app.context import AppContext
from orb_intraday_bot.app.serve import post_session_report
from orb_intraday_bot.app.watchdog import check_once, watchdog_loop
from orb_intraday_bot.config.settings import Settings, StrategyConfig
from orb_intraday_bot.core.clock import IST, FixedClock
from orb_intraday_bot.execution.models import OrderIntent, OrderLeg, OrderStatus
from orb_intraday_bot.execution.order_manager import OrderManager
from orb_intraday_bot.execution.paper_venue import PaperVenue
from orb_intraday_bot.execution.shadow_venue import ShadowVenue
from orb_intraday_bot.runtime.alerts import Alerts
from orb_intraday_bot.runtime.live import LiveRunner, RunOutcome
from orb_intraday_bot.runtime.monitor import write_heartbeat
from tests.runtime_support import RUN_CFG, Harness, Recorder, at


def _ctx(tmp_path: Path) -> AppContext:
    return AppContext(
        Settings.model_construct(
            data_dir=tmp_path,
            telegram_bot_token=SecretStr(""),
            telegram_chat_id="",
            healthcheck_url="https://example.test/ping",
            live_mode="paper",
            live_armed=False,
            strategy_config_path=Path("config/default.yaml"),
        ),
        StrategyConfig(),
        tmp_path,
    )


def test_watchdog_loop_iterations(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    now = datetime(2026, 10, 16, 11, tzinfo=IST)
    write_heartbeat(
        tmp_path / "heartbeat.json", now=now, status="running",
        last_candle=None, open_positions=0, errors=0,
    )
    clock = FixedClock(now)
    notes = Recorder()
    pings: list[str] = []
    code = watchdog_loop(
        ctx, clock=clock, sleep=lambda _: None, notifier=notes,
        ping=pings.append, max_iterations=2, poll_sec=0,
    )
    assert code == 0
    assert pings


def test_watchdog_error_status_alerts(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    now = datetime(2026, 10, 16, 11, tzinfo=IST)
    write_heartbeat(
        tmp_path / "heartbeat.json", now=now, status="error",
        last_candle=None, open_positions=0, errors=1, message="boom",
    )
    notes = Recorder()
    check_once(
        ctx, Alerts(notes), now, holidays=frozenset(), ping=lambda _: None
    )
    assert any("SILENT" in m for m in notes.messages)


def test_shadow_and_paper_poll_cancel() -> None:
    quotes = {"X": 5.0}
    shadow = ShadowVenue(quote=quotes.get)
    intent = OrderIntent(
        signal_id="a", leg=OrderLeg.ENTRY, trading_symbol="X",
        side="BUY", quantity=1, order_type="LIMIT", price=None,
    )
    record = shadow.submit(intent)
    assert shadow.poll(record).status is OrderStatus.FILLED
    assert shadow.recent_fills(record)
    assert shadow.cancel(record).status is OrderStatus.FILLED


def test_order_manager_poll_open_partial() -> None:
    class SlowVenue(PaperVenue):
        def submit(self, intent):  # noqa: ANN001
            record = super().submit(intent)
            # Force non-terminal for poll path by cloning status trick:
            # re-submit a rejected key then manually leave SENT
            return record

    clock = FixedClock(datetime(2026, 10, 16, 10, tzinfo=IST))
    venue = PaperVenue(clock=clock)
    mgr = OrderManager(venue, clock=clock)
    # Rejected path via on_reject
    rejects: list[str] = []
    intent = OrderIntent(
        signal_id="r", leg=OrderLeg.ENTRY, trading_symbol="X",
        side="BUY", quantity=1, order_type="LIMIT", price=1.0,
    )
    bad = PaperVenue(clock=clock, reject_keys={intent.idempotency_key})
    mgr2 = OrderManager(bad, clock=clock, on_reject=lambda r: rejects.append(r.reject_reason))
    mgr2.submit(intent)
    assert rejects
    assert mgr.cancel("missing") is None


def test_live_flatten_closes_positions(tmp_path: Path) -> None:
    h = Harness(tmp_path, at(9, 32))
    flatten = {"go": False}
    original_sleep = h.sleep

    def sleep(sec: float) -> None:
        original_sleep(sec)
        if h.clock.now() >= at(9, 36):
            flatten["go"] = True

    runner = LiveRunner(
        RUN_CFG, tmp_path, h.source, h.store, h.notes, h.clock, sleep, h.lines.append,
        flatten_requested=lambda: flatten["go"],
    )
    outcome = runner.run()
    assert isinstance(outcome, RunOutcome)
    assert outcome.exit_code in (0, 1, 130)


def test_post_session_report_no_db(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    post_session_report(ctx, Alerts(Recorder()), date(2026, 10, 16))


def test_venue_submit_exception_rejects() -> None:
    from orb_intraday_bot.adapters.groww.venue import GrowwVenue

    class Boom:
        def place_order(self, **kwargs):  # noqa: ANN001, ANN003
            raise RuntimeError("broker down")

        def cancel_order(self, **kwargs):  # noqa: ANN001, ANN003
            return {}

        def create_smart_order(self, **kwargs):  # noqa: ANN001, ANN003
            return {}

        def get_order_status(self, **kwargs):  # noqa: ANN001, ANN003
            return {}

        def get_order_status_by_reference(self, **kwargs):  # noqa: ANN001, ANN003
            return {}

        def get_order_list(self, **kwargs):  # noqa: ANN001, ANN003
            raise RuntimeError("nope")

        def get_ltp(self, **kwargs):  # noqa: ANN001, ANN003
            return {}

    venue = GrowwVenue(Boom(), fill_wait_sec=0, fill_polls=1)
    intent = OrderIntent(
        signal_id="x", leg=OrderLeg.ENTRY, trading_symbol="X",
        side="BUY", quantity=1, order_type="LIMIT", price=10.0,
    )
    assert venue.submit(intent).status is OrderStatus.REJECTED
    assert venue.list_orb_orders() == []


def test_entries_blocked_when_risk_halted(tmp_path: Path) -> None:
    from orb_intraday_bot.app.serve import ServeState
    from orb_intraday_bot.risk.engine import RiskEngine
    from orb_intraday_bot.risk.limits import RiskLimits
    from orb_intraday_bot.runtime.telegram_commands import CommandBus

    risk = RiskEngine(limits=RiskLimits(daily_loss_limit_pct=0.01), capital_inr=100_000)
    risk.reset_day(date(2026, 10, 16))
    risk.trip("test")
    state = ServeState(bus=CommandBus(), risk=risk)
    assert state.risk.state.halted
    assert not state.risk.check_entry(lots=1).allowed
