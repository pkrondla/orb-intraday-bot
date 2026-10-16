"""How serve wires the executor: order modes, risk, restart recovery, mid-session commands."""

from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from orb_intraday_bot.app import serve
from orb_intraday_bot.app.context import AppContext
from orb_intraday_bot.app.serve import (
    ServeState,
    build_executor,
    parse_clock_time,
    restore_risk,
    run_one_session,
)
from orb_intraday_bot.config.settings import Settings
from orb_intraday_bot.execution.account import DryRunAccount
from orb_intraday_bot.execution.dry_run_venue import DryRunVenue
from orb_intraday_bot.paper.models import ExitReason, Fill, PositionStatus
from orb_intraday_bot.risk.engine import RiskEngine
from orb_intraday_bot.risk.limits import RiskLimits
from orb_intraday_bot.runtime.executor import LiveExecutor
from orb_intraday_bot.runtime.telegram_commands import Command, CommandBus, CommandKind
from orb_intraday_bot.storage.store import TradeStore
from tests.execution_support import AutoQuotes, FakeAccount
from tests.runtime_support import DAY, RUN_CFG, Harness, at
from tests.unit.test_paper import opened


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "live_mode": "paper", "live_armed": False, "dry_run": True,
        "groww_totp_token": SecretStr("t"), "groww_totp_secret": SecretStr("s"),
        "groww_trading_totp_token": SecretStr(""), "groww_trading_totp_secret": SecretStr(""),
        "telegram_bot_token": SecretStr(""), "telegram_chat_id": "", "healthcheck_url": "",
        "strategy_config_path": Path("config/default.yaml"),
    }
    base.update(overrides)
    return Settings.model_construct(**base)


def _ctx(tmp_path: Path, **overrides: Any) -> AppContext:
    settings = _settings(data_dir=tmp_path, **overrides)
    return AppContext(settings, RUN_CFG, tmp_path)


# ---- small helpers -----------------------------------------------------------------------
def test_parse_clock_time() -> None:
    assert parse_clock_time("15:20") == time(15, 20)
    assert parse_clock_time("09:05") == time(9, 5)


def test_restore_risk_counts_only_executed_trades_of_today(tmp_path: Path) -> None:
    store = TradeStore(tmp_path / "t.db")
    risk = RiskEngine(limits=RiskLimits())
    risk.reset_day(DAY)

    paper = opened()
    paper.position_id = "paper-one"
    store.save_position(paper, at(9, 40))

    losing = opened()
    losing.position_id = "dry-closed"
    losing.order_mode = "dry_run"
    losing.status = PositionStatus.CLOSED
    losing.closed_at = at(10, 0)
    losing.remaining_lots = 0
    losing.fills.append(
        Fill(at(10, 0), ExitReason.HARD_STOP, 6, 390, 22765.0, 100.0, "dry_run",
             -1000.0, -35.0, 0.0)
    )
    store.save_position(losing, at(10, 0))

    running = opened()
    running.position_id = "dry-open"
    running.order_mode = "dry_run"
    store.save_position(running, at(10, 5))

    restore_risk(risk, store, DAY)
    assert risk.state.trades_today == 2                       # the paper trade is not counted
    assert risk.state.open_positions == 1
    assert risk.state.realized_pnl_inr == pytest.approx(losing.net_option_pnl)
    assert risk.state.loss_streak == 1
    assert not risk.state.halted


def test_restore_risk_halts_again_if_the_day_already_breached_the_loss_limit(
    tmp_path: Path,
) -> None:
    store = TradeStore(tmp_path / "t.db")
    risk = RiskEngine(limits=RiskLimits(), capital_inr=500_000)
    risk.reset_day(DAY)
    big = opened()
    big.position_id = "dry-big-loss"
    big.order_mode = "dry_run"
    big.status = PositionStatus.CLOSED
    big.closed_at = at(10, 0)
    big.remaining_lots = 0
    big.fills.append(
        Fill(at(10, 0), ExitReason.HARD_STOP, 6, 390, 22765.0, 100.0, "dry_run",
             -9000.0, -35.0, 0.0)
    )
    store.save_position(big, at(10, 0))
    restore_risk(risk, store, DAY)
    assert risk.state.halted and "daily loss" in risk.state.halt_reason


# ---- building the executor ---------------------------------------------------------------
class _Clients:
    """Stand-ins for the Groww clients: building the executor must not touch the network."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.readonly_built = 0
        self.trading_built = 0
        from orb_intraday_bot.adapters.groww import factory

        def readonly(settings: Settings) -> object:
            self.readonly_built += 1
            return object()

        def trading(settings: Settings) -> object:
            self.trading_built += 1
            return object()

        monkeypatch.setattr(factory, "build_readonly_client", readonly)
        monkeypatch.setattr(factory, "build_trading_client", trading)


def _build(tmp_path: Path, **overrides: Any) -> LiveExecutor | None:
    from orb_intraday_bot.core.clock import FixedClock

    ctx = _ctx(tmp_path, **overrides)
    store = TradeStore(":memory:")
    return build_executor(
        ctx, store=store, risk=RiskEngine(limits=RiskLimits()),
        clock=FixedClock(at(9, 10)), sleep=lambda _: None, notify=lambda _: None,
    )


def test_paper_mode_builds_no_executor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clients = _Clients(monkeypatch)
    assert _build(tmp_path, live_mode="paper") is None
    assert clients.readonly_built == clients.trading_built == 0


def test_shadow_mode_is_a_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _Clients(monkeypatch)
    executor = _build(tmp_path, live_mode="shadow", dry_run=False)   # DRY_RUN is ignored here
    assert executor is not None and executor.mode == "dry_run"
    assert isinstance(executor._venue, DryRunVenue)


def test_live_with_dry_run_never_builds_a_venue_that_can_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clients = _Clients(monkeypatch)
    executor = _build(tmp_path, live_mode="live", live_armed=True, dry_run=True)
    assert executor is not None and executor.mode == "dry_run"
    assert isinstance(executor._venue, DryRunVenue)
    assert isinstance(executor._account, DryRunAccount)
    assert clients.readonly_built == 1                               # quotes, read-only
    assert clients.trading_built == 1                                # funds only, via a read view


def test_live_without_dry_run_builds_the_real_venue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orb_intraday_bot.adapters.groww.account import GrowwAccountReader
    from orb_intraday_bot.adapters.groww.venue import GrowwVenue

    clients = _Clients(monkeypatch)
    executor = _build(tmp_path, live_mode="live", live_armed=True, dry_run=False)
    assert executor is not None and executor.mode == "live"
    assert isinstance(executor._venue, GrowwVenue)
    assert isinstance(executor._account, GrowwAccountReader)
    assert clients.trading_built == 1


# ---- a whole session through run_one_session ---------------------------------------------
class _Rig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **settings: Any) -> None:
        self.h = Harness(tmp_path, at(9, 10))
        self.ctx = _ctx(tmp_path, live_mode="live", live_armed=True, dry_run=True, **settings)
        self.state = ServeState(bus=CommandBus(), risk=RiskEngine(limits=RiskLimits()))
        self.executor: LiveExecutor | None = None
        monkeypatch.setattr(serve, "build_market_data", lambda ctx: self.h.source)
        monkeypatch.setattr(serve, "build_executor", self._build_executor)

    def _build_executor(
        self, ctx: AppContext, *, store: TradeStore, risk: RiskEngine, clock: Any,
        sleep: Any, notify: Any,
    ) -> LiveExecutor:
        quotes = AutoQuotes()
        venue = DryRunVenue(quotes, clock)
        self.executor = LiveExecutor(
            mode="dry_run", venue=venue, quotes=quotes,
            account=DryRunAccount(venue, FakeAccount()), limits=risk.limits, clock=clock,
            sleep=sleep, risk=risk, store=store, notify=notify,
        )
        return self.executor

    def run(self) -> TradeStore:
        outcome = run_one_session(
            self.ctx, self.h.notes, self.state, self.h.clock, self.h.sleep
        )
        assert outcome.exit_code == 0
        return TradeStore(self.ctx.database_path)


def test_a_session_trades_through_the_executor_and_counts_risk_without_any_daily_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    store = rig.run()
    positions = store.positions_for_day(DAY)
    assert positions and all(p.order_mode == "dry_run" for p in positions)
    assert rig.state.risk.state.trades_today == len(positions)
    assert rig.state.risk.state.open_positions == 0
    assert rig.state.risk.state.realized_pnl_inr == pytest.approx(
        sum(p.net_option_pnl for p in positions)
    )

def test_a_session_without_live_arming_runs_as_paper_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    rig.ctx.settings.live_armed = False
    store = rig.run()
    positions = store.positions_for_day(DAY)
    assert positions and all(p.order_mode == "paper" for p in positions)
    assert rig.state.risk.state.trades_today == 0              # paper trades never counted
    assert "Running as PAPER" in rig.h.notes.joined()
    assert store.orders_for_signal(positions[0].position_id) == []


def test_a_stop_from_an_earlier_day_does_not_carry_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    rig.state.bus.stop_for(DAY.replace(day=14))                 # yesterday's stop is long over
    store = rig.run()
    positions = store.positions_for_day(DAY)
    assert positions and all(p.order_mode == "dry_run" for p in positions)


def test_commands_sent_during_the_session_are_acted_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    rig.state.bus.put(Command(CommandKind.STOP_TODAY))               # queued before the session
    store = rig.run()
    assert rig.state.bus.is_stopped(DAY)                                 # drained mid-session
    assert store.positions_for_day(DAY) == []                   # stopped: no entry was taken
    assert "stopped for" in rig.h.notes.joined()                     # the operator got the reply


def test_a_restart_mid_day_restores_the_risk_counters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    first = rig.run()
    trades = len(first.positions_for_day(DAY))
    assert trades >= 1
    # A new process: fresh risk engine and bus, same database and the day already traded.
    rig.state = ServeState(bus=CommandBus(), risk=RiskEngine(limits=RiskLimits()))
    rig.h.clock.set(at(9, 10))
    rig.h.source.candles = []                                   # nothing new to trade
    rig.h.lines.clear()
    outcome = run_one_session(rig.ctx, rig.h.notes, rig.state, rig.h.clock, rig.h.sleep)
    assert outcome.exit_code == 0
    assert rig.state.risk.state.trades_today == trades
