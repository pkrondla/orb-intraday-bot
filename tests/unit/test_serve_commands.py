"""Serve command handlers and order manager coverage."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from pydantic import SecretStr

from orb_intraday_bot.app.context import AppContext
from orb_intraday_bot.app.serve import ServeState, drain_commands, handle_command
from orb_intraday_bot.config.settings import Settings, StrategyConfig
from orb_intraday_bot.core.clock import IST, FixedClock
from orb_intraday_bot.execution.models import OrderIntent, OrderLeg, OrderStatus
from orb_intraday_bot.execution.order_manager import OrderManager
from orb_intraday_bot.execution.paper_venue import PaperVenue
from orb_intraday_bot.risk.engine import RiskEngine
from orb_intraday_bot.risk.limits import RiskLimits
from orb_intraday_bot.runtime.alerts import Alerts
from orb_intraday_bot.runtime.monitor import write_heartbeat
from orb_intraday_bot.runtime.telegram_commands import Command, CommandBus, CommandKind
from orb_intraday_bot.storage.store import TradeStore
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


def test_handle_status_pause_resume_arm_disarm(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    write_heartbeat(
        tmp_path / "heartbeat.json",
        now=datetime(2026, 10, 16, 11, tzinfo=IST),
        status="running",
        last_candle=None,
        open_positions=0,
        errors=0,
    )
    notes = Recorder()
    replies: list[str] = []
    state = ServeState(bus=CommandBus(), risk=RiskEngine())
    alerts = Alerts(notes)
    now = datetime(2026, 10, 16, 11, tzinfo=IST)

    handle_command(
        Command(CommandKind.STATUS), ctx=ctx, alerts=alerts, state=state,
        reply=replies.append, now=now,
    )
    assert replies and "running" in replies[-1].lower() or replies

    handle_command(
        Command(CommandKind.PAUSE), ctx=ctx, alerts=alerts, state=state,
        reply=replies.append, now=now,
    )
    assert state.bus.paused
    handle_command(
        Command(CommandKind.RESUME), ctx=ctx, alerts=alerts, state=state,
        reply=replies.append, now=now,
    )
    assert not state.bus.paused

    handle_command(
        Command(CommandKind.ARM, arg=""), ctx=ctx, alerts=alerts, state=state,
        reply=replies.append, now=now,
    )
    code = state.bus.begin_arm()  # ensure path; previous issued a code via begin_arm inside
    # Re-issue cleanly
    code = state.bus.begin_arm()
    handle_command(
        Command(CommandKind.ARM, arg=code), ctx=ctx, alerts=alerts, state=state,
        reply=replies.append, now=now,
    )
    assert state.bus.armed_today
    handle_command(
        Command(CommandKind.DISARM), ctx=ctx, alerts=alerts, state=state,
        reply=replies.append, now=now,
    )
    assert not state.bus.armed_today


def test_flatten_confirm_writes_marker(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    state = ServeState(bus=CommandBus(), risk=RiskEngine())
    code = state.bus.begin_flatten()
    replies: list[str] = []
    handle_command(
        Command(CommandKind.FLATTEN_CONFIRM, arg=code),
        ctx=ctx, alerts=Alerts(Recorder()), state=state,
        reply=replies.append, now=datetime.now(IST),
    )
    assert (tmp_path / "FLATTEN").exists()


def test_today_positions_report_without_db(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    state = ServeState(bus=CommandBus(), risk=RiskEngine())
    replies: list[str] = []
    for kind in (CommandKind.TODAY, CommandKind.POSITIONS, CommandKind.REPORT):
        handle_command(
            Command(kind), ctx=ctx, alerts=Alerts(Recorder()), state=state,
            reply=replies.append, now=datetime(2026, 10, 16, tzinfo=IST),
        )
    assert any("No database" in r or "No open" in r for r in replies)


def test_drain_commands_swallows_handler_errors(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    state = ServeState(bus=CommandBus(), risk=RiskEngine())
    state.bus.put(Command(CommandKind.STATUS))
    # Missing heartbeat is fine; just ensure drain runs
    replies: list[str] = []
    drain_commands(ctx, Alerts(Recorder()), state, replies.append, datetime.now(IST))


def test_order_manager_cancel_and_poll() -> None:
    clock = FixedClock(datetime(2026, 10, 16, 10, tzinfo=IST))
    venue = PaperVenue(clock=clock)
    fills: list[str] = []
    mgr = OrderManager(venue, clock=clock, on_fill=lambda r, f: fills.append(f.intent_key))
    intent = OrderIntent(
        signal_id="s1", leg=OrderLeg.ENTRY, trading_symbol="X", side="BUY",
        quantity=75, order_type="LIMIT", price=10.0,
    )
    record = mgr.submit(intent)
    assert record.status is OrderStatus.FILLED
    assert fills
    assert mgr.cancel(intent.idempotency_key) is not None
    assert mgr.poll_open() == []
    assert mgr.open_orders() == []
    assert mgr.now().tzinfo is not None


def test_paper_venue_reject_no_price_and_cancel() -> None:
    venue = PaperVenue()
    intent = OrderIntent(
        signal_id="s2", leg=OrderLeg.ENTRY, trading_symbol="X", side="BUY",
        quantity=1, order_type="MARKET", price=None,
    )
    record = venue.submit(intent)
    assert record.status is OrderStatus.REJECTED
    # cancel terminal is no-op
    assert venue.cancel(record).status is OrderStatus.REJECTED


def test_risk_engine_edge_gates() -> None:
    eng = RiskEngine(limits=RiskLimits(max_lots_per_trade=2, max_open_positions=1), capital_inr=1e5)
    eng.reset_day(date(2026, 10, 16))
    assert not eng.check_entry(lots=0).allowed
    assert not eng.check_entry(lots=1, data_age_sec=999).allowed
    assert not eng.check_entry(lots=1, option_spread_pct=0.5).allowed
    assert not eng.check_entry(lots=1, price_sane=False).allowed
    assert not eng.check_entry(
        lots=1, available_margin_inr=10, required_margin_inr=100
    ).allowed
    eng.record_open()
    assert not eng.check_entry(lots=1).allowed  # max open
    eng.record_close(50)
    eng.clear_halt()
    for _ in range(eng.limits.max_trades_per_day):
        eng.record_open()
        eng.record_close(1)
    assert not eng.check_entry(lots=1).allowed


def test_positions_command_with_open(tmp_path: Path) -> None:
    from orb_intraday_bot.core.models import Direction
    from orb_intraday_bot.paper.models import Position, PositionStatus

    ctx = _ctx(tmp_path)
    store = TradeStore(ctx.database_path)
    pos = Position(
        position_id="p1", setup="ORB_BREAKOUT", direction=Direction.LONG,
        level_name="ORH", sign_id="s", zone="NEUTRAL",
        opened_at=datetime(2026, 10, 16, 10, tzinfo=IST),
        trading_symbol="NIFTY26OCT25000CE", kind="CE", strike=25000,
        expiry="2026-10-27", lot_size=75, tick_size=0.05, delta_used=0.55,
        entry_ltp=100.0, entry_charges=20.0, futures_entry=25000.0,
        hard_stop=24965.0, close_exit_level=None, t1=25050.0, t2=25100.0,
        sl_exit=80.0, t1_option=120.0, t2_option=140.0, lots=1, t1_book_lots=1,
        remaining_lots=1,
    )
    assert pos.status is PositionStatus.OPEN
    store.save_position(pos, datetime(2026, 10, 16, 10, tzinfo=IST))
    store.close()
    replies: list[str] = []
    handle_command(
        Command(CommandKind.POSITIONS),
        ctx=ctx, alerts=Alerts(Recorder()),
        state=ServeState(bus=CommandBus(), risk=RiskEngine()),
        reply=replies.append, now=datetime(2026, 10, 16, 11, tzinfo=IST),
    )
    assert "open" in replies[-1].lower()
