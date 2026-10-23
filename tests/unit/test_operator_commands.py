"""The operator commands: /risk /orders /signals /pnl /progress /levels /funds /mode /version /log
and the acting ones /exit /rearm /reconcile /clearhalt /restart."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from orb_intraday_bot.app import operator_text as ot
from orb_intraday_bot.app import serve
from orb_intraday_bot.app.context import AppContext
from orb_intraday_bot.app.serve import ServeState, handle_command
from orb_intraday_bot.engine.signal_engine import PlannedTrade
from orb_intraday_bot.execution.models import OrderIntent, OrderLeg, OrderRecord, OrderStatus
from orb_intraday_bot.indicators.pivots import SessionLevels
from orb_intraday_bot.options.pricing import OptionPlan
from orb_intraday_bot.paper.models import ExitReason, PositionStatus
from orb_intraday_bot.risk.engine import RiskEngine
from orb_intraday_bot.risk.limits import RiskLimits
from orb_intraday_bot.runtime.alerts import Alerts
from orb_intraday_bot.runtime.live import LiveRunner, RunOutcome
from orb_intraday_bot.runtime.telegram_commands import Command, CommandBus, CommandKind
from orb_intraday_bot.storage.store import TradeStore
from tests.runtime_support import DAY, Recorder, at
from tests.unit.test_live_runner_execution import Session, _primed
from tests.unit.test_paper import make_trade, opened
from tests.unit.test_serve_execution import _ctx

NOW = at(10, 0)


def _another_trade() -> tuple[PlannedTrade, OptionPlan]:
    """A second planned trade with its own signal id (the id comes from the candle time)."""
    trade, plan = make_trade()
    return replace(trade, signal=replace(trade.signal, candle_start=at(9, 45))), plan


def _state(runner: LiveRunner | None = None, risk: RiskEngine | None = None) -> ServeState:
    engine = risk or RiskEngine(limits=RiskLimits())
    engine.reset_day(DAY)
    return ServeState(bus=CommandBus(), risk=engine, runner=runner)


def _send(
    kind: CommandKind, arg: str, *, ctx: AppContext, state: ServeState,
    now: datetime = NOW, notes: Recorder | None = None,
) -> list[str]:
    replies: list[str] = []
    handle_command(
        Command(kind, arg=arg), ctx=ctx, alerts=Alerts(notes or Recorder()), state=state,
        reply=replies.append, now=now,
    )
    return replies


def _database(ctx: AppContext) -> TradeStore:
    """A file database with one signal, one dry-run position and its entry order."""
    store = TradeStore(ctx.database_path)
    trade, _ = make_trade()
    store.record_signal(trade.signal, "accepted")
    position = opened()
    position.order_mode = "dry_run"
    position.planned_entry = 150.0
    position.entry_ltp = 151.5
    position.stop_order_id = "STOP-1"
    position.stop_trigger = 100.0
    position.stop_quantity = position.remaining_quantity
    store.save_position(position, at(9, 40))
    intent = OrderIntent(
        position.position_id, OrderLeg.ENTRY, position.trading_symbol, "BUY",
        position.quantity, "LIMIT", price=152.0,
    )
    store.save_order(
        OrderRecord(
            intent, OrderStatus.FILLED, "B-1", filled_qty=position.quantity, avg_price=151.5,
            mode="dry_run", created_at=at(9, 36), updated_at=at(9, 36),
        )
    )
    return store


# ---- parsing -------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "kind", "arg"),
    [
        ("/risk", CommandKind.RISK, ""),
        ("/orders", CommandKind.ORDERS, ""),
        ("/signals", CommandKind.SIGNALS, ""),
        ("/levels", CommandKind.LEVELS, ""),
        ("/funds", CommandKind.FUNDS, ""),
        ("/pnl week", CommandKind.PNL, "week"),
        ("/progress", CommandKind.PROGRESS, ""),
        ("/mode", CommandKind.MODE, ""),
        ("/version", CommandKind.VERSION, ""),
        ("/log 30", CommandKind.LOG, "30"),
        ("/logs", CommandKind.LOG, ""),
        ("/exit NIFTY26O1925000CE ab12", CommandKind.EXIT, "NIFTY26O1925000CE ab12"),
        ("/rearm", CommandKind.REARM, ""),
        ("/reconcile", CommandKind.RECONCILE, ""),
        ("/clearhalt", CommandKind.CLEARHALT, ""),
        ("/restart ab12", CommandKind.RESTART, "ab12"),
        ("/risk@my_bot", CommandKind.RISK, ""),
    ],
)
def test_new_commands_are_parsed(text: str, kind: CommandKind, arg: str) -> None:
    from orb_intraday_bot.runtime.telegram_commands import parse_command

    command = parse_command(text)
    assert (command.kind, command.arg) == (kind, arg)


def test_the_help_asks_for_no_codes_and_explains_each_command() -> None:
    from orb_intraday_bot.runtime.telegram_commands import _HELP

    lines = {ln.split()[0]: ln for ln in _HELP.splitlines() if ln.startswith("/")}
    for word in (
        "/status", "/mode", "/version", "/risk", "/funds", "/log", "/today", "/pnl",
        "/positions", "/progress", "/signals", "/orders", "/levels", "/report", "/stop",
        "/start", "/exit", "/flatten", "/rearm", "/reconcile", "/clearhalt", "/restart",
        "/help", "/dryrun", "/limits", "/capital", "/riskpct", "/dailyloss", "/maxlots",
    ):
        assert " - " in lines[word]                       # every command has its purpose
    assert "code" not in _HELP.lower()
    assert len(_HELP) < 4000                              # one Telegram message


# ---- pure text ---------------------------------------------------------------------------
def test_risk_text_says_what_is_blocking_trades() -> None:
    risk = RiskEngine(limits=RiskLimits())
    risk.reset_day(DAY)
    ok = ot.risk_text(risk, stopped_today=False)
    assert "Kill switch: ok" in ok and "Entries blocked: no" in ok
    assert "Trades today: 0 of 3" in ok and "Stopped by /stop today: no" in ok
    risk.trip("daily loss limit hit")
    risk.block_entries("reconciliation mismatch")
    bad = ot.risk_text(risk, stopped_today=True)
    assert "HALTED - daily loss limit hit" in bad
    assert "YES - reconciliation mismatch" in bad
    assert "Stopped by /stop today: yes" in bad
    assert "HALTED" in ot.risk_line(risk) and "ENTRIES BLOCKED" in ot.risk_line(risk)


def test_mode_and_version_text() -> None:
    ctx = _ctx(Path("."), live_mode="live", live_armed=True, dry_run=True)
    risk = RiskEngine(limits=RiskLimits())
    text = ot.mode_text(ctx.settings, risk, stopped_today=False, executor_mode="dry_run")
    assert "LIVE_MODE=live" in text and "DRY_RUN=true" in text
    assert "Session executor: DRY RUN" in text and "Trading today: on" in text
    none = ot.mode_text(
        _ctx(Path(".")).settings, risk, stopped_today=True, executor_mode=None
    )
    assert "Session executor: none" in none and "STOPPED by /stop" in none
    version = ot.version_text(ctx.cfg)
    assert "orb-intraday-bot v" in version and "rules " in version and "python " in version


def test_funds_text_cases() -> None:
    full = ot.funds_text(
        capital_inr=500_000, daily_loss_limit_inr=5_000, mode_label="live", margin=123_456.0,
        margin_note="", held={"A": 75, "B": 0}, held_label="Broker positions",
    )
    assert "Rs 500,000" in full and "Rs 123,456" in full and "A 75" in full and "B 0" not in full
    unknown = ot.funds_text(
        capital_inr=1, daily_loss_limit_inr=1, mode_label="x", margin=None,
        margin_note="margin unknown", held={}, held_label="Ledger",
    )
    assert "margin unknown" in unknown and "Ledger: none" in unknown


def test_progress_text_for_paper_and_executed_positions() -> None:
    assert ot.progress_text([]) == "No open positions."
    paper = opened()
    executed = opened()
    executed.order_mode = "dry_run"
    executed.planned_entry = 150.0
    executed.stop_order_id = "S-9"
    executed.stop_trigger = 99.5
    executed.stop_quantity = 75
    executed.unbooked_qty = 25
    bare = opened()
    bare.order_mode = "live"
    text = ot.progress_text([paper, executed, bare])
    assert "3 open" in text and "[paper]" in text and "[dry_run]" in text
    assert "broker stop: order S-9, trigger 99.50, qty 75" in text
    assert "broker stop: NONE" in text and "25 unit(s)" in text
    assert "planned 150.00" in text
    assert f"T2 {paper.t2:.1f} (trigger {paper.t2_hit_level:.1f})" in text


def test_levels_text_before_and_after_the_session_loads_them() -> None:
    assert "not loaded yet" in ot.levels_text(None, None, None, None, None)
    levels = SessionLevels(DAY, 22900.0, 22600.0, 22750.0)
    text = ot.levels_text("NIFTY26OCTFUT", levels, 22830.0, 22790.0, 22820.0)
    assert "NIFTY26OCTFUT" in text and "R1" in text and "S1" in text and "P " in text
    assert "high 22830.0, low 22790.0 (width 40.0)" in text and "Last candle close: 22820.0" in text
    assert "not complete yet" in ot.levels_text("X", levels, None, None, None)
    ordered = text.splitlines()[1]
    assert ordered.index("R5") < ordered.index("P ") < ordered.index("S5")


def test_period_and_line_parsing() -> None:
    assert ot.parse_pnl_period("") == "day" and ot.parse_pnl_period("Today") == "day"
    assert ot.parse_pnl_period("w") == "week" and ot.parse_pnl_period("MONTH") == "month"
    assert ot.parse_pnl_period("year") is None
    assert ot.parse_log_lines("") == 15 and ot.parse_log_lines("x") == 15
    assert ot.parse_log_lines("0") == 1 and ot.parse_log_lines("500") == 40


def test_match_positions_by_exact_symbol_or_part() -> None:
    a, b = opened(), opened()
    a.trading_symbol, b.trading_symbol = "NIFTY26O1925000CE", "NIFTY26O1924900PE"
    assert ot.match_positions([a, b], "nifty26o1925000ce") == [a]
    assert ot.match_positions([a, b], "24900") == [b]
    assert ot.match_positions([a, b], "NIFTY") == [a, b]
    assert ot.match_positions([a, b], "") == [] and ot.match_positions([a, b], "XYZ") == []


def test_log_tail_redacts_secrets_and_limits_size(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    token = "bot123456789:" + "A" * 35
    (logs / f"orb-bot-{DAY.isoformat()}.log").write_text(
        "\n".join(
            [f"line {i}" for i in range(100)]
            + [f"GET https://api.telegram.org/{token}/getUpdates failed",
               "token=abcdef0123456789 secret: hunter2 Bearer abc.def.ghi", "x" * 1000]
        ),
        encoding="utf-8",
    )
    text = ot.log_tail(tmp_path, DAY, 5)
    assert text.startswith("Last 5 log line(s)")
    assert token not in text and "hunter2" not in text and "abc.def.ghi" not in text
    assert "<redacted>" in text
    assert max(len(line) for line in text.splitlines()) <= 300 + 5
    assert "No log for" in ot.log_tail(tmp_path, DAY - timedelta(days=3), 5)
    (logs / "orb-bot-2026-10-01.log").write_text("", encoding="utf-8")
    assert "empty" in ot.log_tail(tmp_path, DAY.replace(day=1), 5)


# ---- the read-only commands through handle_command ---------------------------------------
def test_reports_without_a_database_say_so(tmp_path: Path) -> None:
    ctx, state = _ctx(tmp_path), _state()
    for kind in (CommandKind.ORDERS, CommandKind.SIGNALS, CommandKind.PNL, CommandKind.PROGRESS):
        assert _send(kind, "", ctx=ctx, state=state) == ["No database yet."]
    assert _send(CommandKind.PNL, "year", ctx=ctx, state=state) == ["Usage: /pnl [day|week|month]"]


def test_reports_read_the_database(tmp_path: Path) -> None:
    ctx, state = _ctx(tmp_path), _state()
    _database(ctx).close()
    orders = _send(CommandKind.ORDERS, "", ctx=ctx, state=state, now=at(10, 0))[0]
    assert "Orders on 2026-10-15: 1" in orders and "ENTRY BUY" in orders
    assert "FILLED" in orders and "[dry_run]" in orders and "filled 390@151.50" in orders
    assert "Entry vs plan" in orders and "planned 150.00, filled 151.50 (+1.0%)" in orders
    signals = _send(CommandKind.SIGNALS, "", ctx=ctx, state=state)[0]
    assert "Signals on 2026-10-15: 1 (1 accepted)" in signals and "ORB_BREAKOUT" in signals
    progress = _send(CommandKind.PROGRESS, "", ctx=ctx, state=state)[0]
    assert "1 open" in progress and "broker stop: order STOP-1" in progress
    pnl = _send(CommandKind.PNL, "", ctx=ctx, state=state)[0]
    assert pnl.startswith("P&L for 2026-10-15") and "1 trade(s)" in pnl and "dry_run x1" in pnl
    for period, head in (("week", "P&L 2026-10-09 to 2026-10-15"), ("month", "P&L 2026-10-01")):
        text = _send(CommandKind.PNL, period, ctx=ctx, state=state)[0]
        assert text.startswith(head) and "2026-10-15: 1 trade(s)" in text
    empty = _send(CommandKind.ORDERS, "", ctx=ctx, state=state, now=at(10, 0, DAY.replace(day=14)))
    assert empty == ["No orders on 2026-10-14."]
    assert _send(
        CommandKind.SIGNALS, "", ctx=ctx, state=state, now=at(10, 0, DAY.replace(day=14))
    ) == ["No signals recorded on 2026-10-14."]
    nothing = _send(CommandKind.PNL, "", ctx=ctx, state=state, now=at(10, 0, DAY.replace(day=14)))
    assert nothing == ["P&L for 2026-10-14\nNo trades."]


def test_the_orders_report_caps_its_length(tmp_path: Path) -> None:
    store = TradeStore(tmp_path / "x.db")
    for i in range(40):
        intent = OrderIntent(
            f"sig{i}", OrderLeg.ENTRY, "SYM", "BUY", 75, "MARKET",
        )
        store.save_order(
            OrderRecord(
                intent, OrderStatus.REJECTED, reject_reason="no funds", mode="live",
                created_at=at(9, 36), updated_at=at(9, 36),
            )
        )
    text = ot.orders_text(store, DAY)
    assert "Orders on 2026-10-15: 40" in text and "(last 25 shown)" in text
    assert text.count("REJECTED") == 25 and "at market" in text and "(no funds)" in text


def test_risk_mode_version_and_log_commands(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, live_mode="live", live_armed=True, dry_run=True)
    state = _state()
    state.bus.stop_for(DAY)
    assert "Stopped by /stop today: yes" in _send(CommandKind.RISK, "", ctx=ctx, state=state)[0]
    assert "LIVE_MODE=live" in _send(CommandKind.MODE, "", ctx=ctx, state=state)[0]
    assert "orb-intraday-bot v" in _send(CommandKind.VERSION, "", ctx=ctx, state=state)[0]
    assert "No log for" in _send(CommandKind.LOG, "5", ctx=ctx, state=state)[0]


def test_levels_command_uses_what_the_session_loaded(tmp_path: Path) -> None:
    ctx, state = _ctx(tmp_path), _state()
    assert "not loaded yet" in _send(CommandKind.LEVELS, "", ctx=ctx, state=state)[0]
    state.levels = ("NIFTY26OCTFUT", SessionLevels(DAY, 22900.0, 22600.0, 22750.0))
    state.levels_day = DAY - timedelta(days=1)                # yesterday's: not today's
    assert "not loaded yet" in _send(CommandKind.LEVELS, "", ctx=ctx, state=state)[0]
    state.levels_day = DAY
    text = _send(CommandKind.LEVELS, "", ctx=ctx, state=state)[0]
    assert "NIFTY26OCTFUT" in text and "Opening range: not complete yet" in text


def test_status_adds_mode_trading_state_session_and_risk(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, live_mode="live", live_armed=True, dry_run=True)
    risk = RiskEngine(limits=RiskLimits())
    risk.reset_day(DAY)
    risk.record_open()
    text = ot.status_extra(ctx.settings, risk, stopped_today=True, session_status="running")
    assert "DRY RUN" in text and "STOPPED by /stop" in text and "Session: running" in text
    assert "trades 1/3" in text and "open 1/1" in text
    idle = ot.status_extra(ctx.settings, risk, stopped_today=False, session_status=None)
    assert "Trading today: on" in idle and "Session: not running" in idle


# ---- /funds ------------------------------------------------------------------------------
class _Reader:
    def __init__(self, margin: float | None = 98_765.0, fail: bool = False) -> None:
        self.margin, self.fail = margin, fail

    def available_option_margin(self) -> float | None:
        if self.fail:
            raise RuntimeError("boom")
        return self.margin

    def net_quantities(self) -> dict[str, int] | None:
        return {}


def test_funds_in_each_situation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state()
    paper = _send(CommandKind.FUNDS, "", ctx=_ctx(tmp_path), state=state)[0]
    assert "not read in paper mode" in paper and "Rs 500,000" in paper
    nocreds = _ctx(tmp_path, live_mode="shadow")
    nocreds.settings.groww_totp_token = type(nocreds.settings.groww_totp_token)("")
    assert "no trading credentials" in _send(CommandKind.FUNDS, "", ctx=nocreds, state=state)[0]

    ctx = _ctx(tmp_path, live_mode="live", live_armed=True)
    monkeypatch.setattr(serve, "build_funds_reader", lambda c: _Reader())
    assert "Rs 98,765" in _send(CommandKind.FUNDS, "", ctx=ctx, state=state)[0]
    monkeypatch.setattr(serve, "build_funds_reader", lambda c: _Reader(fail=True))
    failed = _send(CommandKind.FUNDS, "", ctx=ctx, state=state)[0]
    assert "could not be read (RuntimeError)" in failed

    s, runner, _, held = _primed(tmp_path, at_time=at(10, 0))
    live_state = _state(runner)
    text = _send(CommandKind.FUNDS, "", ctx=ctx, state=live_state)[0]
    assert "Rs 1,000,000" in text and "Dry-run ledger" in text
    assert f"NIFTY26O19CE {held.remaining_quantity}" in text
    s.executor._account.available_option_margin = lambda: None   # type: ignore[method-assign]
    assert "unknown" in _send(CommandKind.FUNDS, "", ctx=ctx, state=live_state)[0]


# ---- the acting commands -----------------------------------------------------------------
def test_acting_commands_need_a_running_session(tmp_path: Path) -> None:
    ctx, state = _ctx(tmp_path), _state()
    for kind in (CommandKind.EXIT, CommandKind.REARM, CommandKind.RECONCILE):
        assert "No session is running" in _send(kind, "X", ctx=ctx, state=state)[0]


def test_acting_commands_in_paper_mode_do_nothing_harmful(tmp_path: Path) -> None:
    from tests.runtime_support import Harness

    h = Harness(tmp_path, at(10, 0))
    state = _state(h.runner())
    ctx = _ctx(tmp_path)
    for kind in (CommandKind.REARM, CommandKind.RECONCILE):
        assert "places no orders" in _send(kind, "", ctx=ctx, state=state)[0]


def test_exit_closes_one_position_and_reports_it(tmp_path: Path) -> None:
    s, runner, _, position = _primed(tmp_path, at_time=at(10, 0))
    ctx, state = _ctx(tmp_path), _state(runner, s.risk)
    assert "Usage: /exit <symbol>" in _send(CommandKind.EXIT, "", ctx=ctx, state=state)[0]
    nope = _send(CommandKind.EXIT, "NOPE", ctx=ctx, state=state)[0]
    assert "No open position matches NOPE" in nope
    notes = Recorder()
    done = _send(CommandKind.EXIT, "ce", ctx=ctx, state=state, notes=notes)
    assert done == [f"{position.trading_symbol}: closed (the exit message has the prices)."]
    assert not position.is_open
    assert [f.reason for f in position.fills] == [ExitReason.MANUAL]
    again = _send(CommandKind.EXIT, position.trading_symbol, ctx=ctx, state=state)[0]
    assert "No open position matches" in again


def test_exit_that_cannot_sell_says_the_position_is_still_open(tmp_path: Path) -> None:
    from tests.unit.test_live_runner_execution import _SellsRest

    s, runner, _, position = _primed(tmp_path, at_time=at(10, 0))
    assert isinstance(s.venue, _SellsRest)
    s.venue.block_sells = True
    reply = _send(
        CommandKind.EXIT, position.trading_symbol, ctx=_ctx(tmp_path), state=_state(runner, s.risk)
    )[0]
    assert "did not complete" in reply and position.is_open


def test_exit_of_a_real_position_needs_no_code(tmp_path: Path) -> None:
    s, runner, _, position = _primed(tmp_path, at_time=at(10, 0))
    position.order_mode = "live"
    ctx, state = _ctx(tmp_path), _state(runner, s.risk)
    done = _send(CommandKind.EXIT, position.trading_symbol, ctx=ctx, state=state)[0]
    assert "closed" in done and not position.is_open

def test_exit_with_an_ambiguous_symbol_asks_for_more(tmp_path: Path) -> None:
    s, runner, trading, position = _primed(tmp_path, at_time=at(10, 0))
    other = opened()
    other.position_id = "other"
    other.trading_symbol = "NIFTY26O1925100PE"
    other.order_mode = "dry_run"
    trading.paper._open[other.position_id] = other
    reply = _send(
        CommandKind.EXIT, "NIFTY", ctx=_ctx(tmp_path), state=_state(runner, s.risk)
    )[0]
    assert "more than one position" in reply and position.is_open and other.is_open


def test_rearm_puts_the_broker_stop_back(tmp_path: Path) -> None:
    s, runner, _, position = _primed(tmp_path, at_time=at(10, 0))
    ctx, state = _ctx(tmp_path), _state(runner, s.risk)
    assert position.stop_order_id                              # placed when the trade opened
    position.stop_order_id = ""                                # as if it had gone missing
    reply = _send(CommandKind.REARM, "", ctx=ctx, state=state)[0]
    assert "broker stop in place" in reply and position.stop_order_id
    runner.exit_positions({position.position_id})
    assert _send(CommandKind.REARM, "", ctx=ctx, state=state) == [
        "No open positions that hold orders."
    ]


def test_reconcile_reports_a_match_and_a_mismatch(tmp_path: Path) -> None:
    s, runner, _, position = _primed(tmp_path, at_time=at(10, 0))
    ctx, state = _ctx(tmp_path), _state(runner, s.risk)
    assert "The books match (1 open position(s) checked)" in _send(
        CommandKind.RECONCILE, "", ctx=ctx, state=state
    )[0]
    position.remaining_lots += 1                               # the bot thinks it holds more
    bad = _send(CommandKind.RECONCILE, "", ctx=ctx, state=state)[0]
    assert "MISMATCH" in bad and "the bot holds" in bad
    assert s.risk.state.entries_blocked


def test_clearhalt_clears_both_flags(tmp_path: Path) -> None:
    ctx, state = _ctx(tmp_path), _state()
    assert "Nothing to clear" in _send(CommandKind.CLEARHALT, "", ctx=ctx, state=state)[0]
    state.risk.trip("loss streak 2")
    state.risk.block_entries("reconciliation mismatch")
    notes = Recorder()
    done = _send(CommandKind.CLEARHALT, "", ctx=ctx, state=state, notes=notes)[0]
    assert done.startswith("Cleared") and "loss streak 2" in done
    assert "reconciliation mismatch" in done
    assert not state.risk.state.halted and not state.risk.state.entries_blocked
    assert "cleared by the operator" in notes.joined()

def test_restart_stops_the_session_and_the_service(tmp_path: Path) -> None:
    s = Session(tmp_path, at(9, 10))
    runner = s.runner()
    ctx, state = _ctx(tmp_path), _state(runner, s.risk)
    notes = Recorder()
    reply = _send(CommandKind.RESTART, "", ctx=ctx, state=state, notes=notes)[0]
    assert reply.startswith("Restarting")
    assert state.stop_requested and "restart requested" in notes.joined()
    outcome = runner.run()                                     # the session ends at once
    assert outcome == RunOutcome(130, "stopped on request")
    assert "Stopped on request" in s.h.text


def test_restart_between_sessions_just_stops_the_service(tmp_path: Path) -> None:
    ctx, state = _ctx(tmp_path), _state()
    _send(CommandKind.RESTART, "", ctx=ctx, state=state)
    assert state.stop_requested and serve.should_stop(tmp_path, state)


def test_control_commands_leave_an_audit_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ctx, state = _ctx(tmp_path), _state()
    with caplog.at_level("INFO", logger="orb.serve"):
        _send(CommandKind.STOP_TODAY, "", ctx=ctx, state=state)
        _send(CommandKind.RISK, "", ctx=ctx, state=state)
    audited = [r for r in caplog.records if getattr(r, "event", "") == "operator_command"]
    assert [getattr(r, "command", "") for r in audited] == ["stop_today"]   # not the read-only one


# ---- the runner and engine pieces they rely on --------------------------------------------
def test_the_runner_reconciles_and_protects_on_request(tmp_path: Path) -> None:
    s, runner, _, position = _primed(tmp_path, at_time=at(10, 0))
    assert runner.reconcile_now() == []
    assert runner.open_positions() == [position]
    assert runner.executor is s.executor and runner.trading_day is not None
    assert runner.protect_open_positions() == [position]
    assert runner.status in {"starting", "running", "waiting"}


def test_the_runner_has_nothing_to_do_without_a_day_or_an_executor(tmp_path: Path) -> None:
    from tests.runtime_support import Harness

    h = Harness(tmp_path, at(10, 0))
    runner = h.runner()
    assert runner.open_positions() == [] and runner.exit_positions({"x"}) == []
    assert runner.protect_open_positions() == [] and runner.reconcile_now() is None
    s = Session(tmp_path, at(10, 0))
    assert s.runner().reconcile_now() is None                  # no day yet


def test_a_spent_flatten_request_does_not_linger(tmp_path: Path) -> None:
    s, runner, trading, position = _primed(tmp_path, at_time=at(10, 0))
    runner.exit_positions({position.position_id})
    assert not trading.paper.open_positions
    marker = s.h.data_dir / "FLATTEN"
    marker.write_text("operator\n", encoding="utf-8")
    runner._flatten_requested = lambda: True
    runner._maybe_flatten(trading)
    assert not marker.exists()                                 # nothing to close: request spent
    # ...so a position opened later is not flattened by a stale request, and a new one works.
    runner._flatten_requested = lambda: marker.exists()
    trade, plan = _another_trade()
    later = trading.paper.open(trade, plan, at(10, 30))
    assert later is not None
    runner._maybe_flatten(trading)
    assert later.is_open
    marker.write_text("operator\n", encoding="utf-8")
    runner._maybe_flatten(trading)
    assert not later.is_open and not marker.exists()


def test_close_all_can_be_limited_to_some_positions(tmp_path: Path) -> None:
    s, runner, trading, first = _primed(tmp_path, at_time=at(10, 0))
    trade, plan = _another_trade()
    second = trading.paper.open(trade, plan, at(10, 5))
    assert second is not None and second.position_id != first.position_id
    events = trading.paper.close_all(
        at(10, 10), 22800.0, None, ExitReason.MANUAL, position_ids={first.position_id}
    )
    assert len(events) == 1 and not first.is_open and second.is_open
    assert second.status is PositionStatus.OPEN
