"""Order modes, the per-day /stop, risk blocking and rebuilding, schema v3 and the order store."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from orb_intraday_bot.config.settings import Settings
from orb_intraday_bot.core.clock import IST
from orb_intraday_bot.execution.models import OrderIntent, OrderLeg, OrderRecord, OrderStatus
from orb_intraday_bot.risk.engine import RiskEngine
from orb_intraday_bot.risk.limits import RiskLimits
from orb_intraday_bot.runtime.arming import (
    configured_mode_label,
    describe_order_mode,
    resolve_mode,
)
from orb_intraday_bot.runtime.telegram_commands import CommandBus
from orb_intraday_bot.storage import migrations
from orb_intraday_bot.storage.store import TradeStore

DAY = date(2026, 10, 16)
NOW = datetime(2026, 10, 16, 10, tzinfo=IST)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "live_mode": "paper", "live_armed": False, "dry_run": True,
        "groww_totp_token": SecretStr("t"), "groww_totp_secret": SecretStr("s"),
        "groww_trading_totp_token": SecretStr(""), "groww_trading_totp_secret": SecretStr(""),
    }
    base.update(overrides)
    return Settings.model_construct(**base)


# ---- arming and order modes --------------------------------------------------------------
@pytest.mark.parametrize(
    ("settings", "creds", "mode", "order_mode"),
    [
        ({"live_mode": "paper"}, True, "paper", "paper"),
        ({"live_mode": "shadow"}, True, "shadow", "dry_run"),
        ({"live_mode": "shadow", "dry_run": False}, True, "shadow", "dry_run"),
        ({"live_mode": "live", "live_armed": False}, True, "paper", "paper"),
        ({"live_mode": "live", "live_armed": True}, False, "paper", "paper"),
        ({"live_mode": "live", "live_armed": True}, True, "live", "dry_run"),
        ({"live_mode": "live", "live_armed": True, "dry_run": False}, True, "live", "live"),
    ],
)
def test_the_order_mode_follows_every_gate(
    settings: dict[str, Any], creds: bool, mode: str, order_mode: str
) -> None:
    decision = resolve_mode(_settings(**settings), trading_credentials_present=creds)
    assert (decision.mode, decision.order_mode) == (mode, order_mode)
    assert decision.is_live is (mode == "live")


def test_a_missing_or_misspelt_dry_run_never_means_real_orders() -> None:
    assert Settings(_env_file=None).dry_run is True          # type: ignore[call-arg]


def test_labels_say_what_will_happen() -> None:
    assert "paper" in configured_mode_label(_settings())
    assert "shadow" in configured_mode_label(_settings(live_mode="shadow"))
    assert "DRY RUN" in configured_mode_label(_settings(live_mode="live"))
    assert "real orders" in configured_mode_label(_settings(live_mode="live", dry_run=False))
    assert "DRY RUN" in describe_order_mode("dry_run")
    assert "LIVE" in describe_order_mode("live")
    assert "PAPER" in describe_order_mode("paper")
    assert describe_order_mode("odd") == "odd"


# ---- /stop and /start are for one day ----------------------------------------------------
def test_a_stop_is_valid_for_its_own_day_only() -> None:
    bus = CommandBus()
    assert not bus.is_stopped(DAY)                  # trading is on by default
    bus.stop_for(DAY)
    assert bus.is_stopped(DAY)
    assert not bus.is_stopped(date(2026, 10, 19))   # the next day starts on its own
    bus.start_for(date(2026, 10, 19))               # starting another day does not clear this one
    assert bus.is_stopped(DAY)
    bus.start_for(DAY)
    assert not bus.is_stopped(DAY) and bus.stopped_day is None


# ---- risk ---------------------------------------------------------------------------------
def _risk() -> RiskEngine:
    risk = RiskEngine(limits=RiskLimits(), capital_inr=500_000)
    risk.reset_day(DAY)
    return risk


def test_blocking_entries_stops_new_trades_without_a_flatten() -> None:
    risk = _risk()
    risk.block_entries("reconciliation mismatch")
    decision = risk.check_entry(lots=1)
    assert not decision.allowed and "reconciliation" in decision.reason
    assert not risk.state.halted                              # nothing is flattened
    risk.clear_halt()
    assert risk.check_entry(lots=1).allowed


def test_rebuild_restores_counters_and_streaks() -> None:
    risk = _risk()
    risk.rebuild(trades_today=3, open_positions=1, closed_pnls=[500.0, -200.0])
    assert risk.state.trades_today == 3 and risk.state.open_positions == 1
    assert risk.state.realized_pnl_inr == 300.0 and risk.state.loss_streak == 1
    assert not risk.state.halted


def test_rebuild_halts_on_a_loss_streak_or_the_daily_loss_limit() -> None:
    streak = _risk()
    streak.rebuild(trades_today=2, open_positions=0, closed_pnls=[-100.0, -100.0])
    assert streak.state.halted and "streak" in streak.state.halt_reason
    limit = _risk()
    limit.rebuild(trades_today=1, open_positions=0, closed_pnls=[-6000.0])
    assert limit.state.halted and "daily loss" in limit.state.halt_reason


def test_a_win_resets_the_loss_streak_in_a_rebuild() -> None:
    risk = _risk()
    risk.rebuild(trades_today=3, open_positions=0, closed_pnls=[-100.0, 50.0, -100.0])
    assert risk.state.loss_streak == 1 and not risk.state.halted


# ---- schema v3 and the store --------------------------------------------------------------
def _intent(attempt: int = 1, leg: OrderLeg = OrderLeg.ENTRY) -> OrderIntent:
    return OrderIntent(
        signal_id="sig-abcdef12", leg=leg, trading_symbol="NIFTY26O19CE", side="BUY",
        quantity=75, order_type="LIMIT", price=150.0, trigger_price=None, attempt=attempt,
    )


def test_upgrading_a_v2_database_adds_the_mode_columns(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(migrations._MIGRATIONS[1])               # noqa: SLF001
    conn.executescript(migrations._MIGRATIONS[2])               # noqa: SLF001
    conn.execute("INSERT INTO schema_meta VALUES ('schema_version', '2')")
    conn.execute(
        "INSERT INTO orders (idempotency_key, signal_id, leg, trading_symbol, side, quantity,"
        " order_type, product, status, created_at, updated_at)"
        " VALUES ('s:ENTRY:1','s','ENTRY','X','BUY',75,'LIMIT','MIS','FILLED','t','t')"
    )
    conn.commit()
    conn.close()
    store = TradeStore(path)
    row = store._conn.execute("SELECT mode FROM orders").fetchone()   # noqa: SLF001
    assert row["mode"] == "paper"                               # old rows were paper orders
    assert migrations.current_version(store._conn) == migrations.SCHEMA_VERSION   # noqa: SLF001


def test_orders_and_events_carry_their_mode(tmp_path: Path) -> None:
    store = TradeStore(tmp_path / "t.db")
    record = OrderRecord(intent=_intent(), mode="dry_run", created_at=NOW, updated_at=NOW)
    record.transition(OrderStatus.SENT)
    store.save_order(record)
    store.append_event("order_sent", {"k": 1}, signal_id="sig-abcdef12", at=NOW, mode="dry_run")
    store.append_event("note", {"k": 2})
    assert store.orders_for_signal("sig-abcdef12")[0]["mode"] == "dry_run"
    modes = [r["mode"] for r in store._conn.execute("SELECT mode FROM events ORDER BY id")]   # noqa: SLF001
    assert modes == ["dry_run", ""]


def test_attempts_used_and_load_order_round_trip(tmp_path: Path) -> None:
    store = TradeStore(tmp_path / "t.db")
    assert store.attempts_used("sig-abcdef12", "ENTRY") == 0
    for attempt in (1, 2):
        rec = OrderRecord(intent=_intent(attempt), mode="live", created_at=NOW, updated_at=NOW)
        rec.broker_order_id = f"G-{attempt}"
        rec.transition(OrderStatus.SENT)
        rec.transition(OrderStatus.ACKNOWLEDGED)
        store.save_order(rec)
    assert store.attempts_used("sig-abcdef12", "ENTRY") == 2
    assert store.attempts_used("sig-abcdef12", "PROTECT") == 0
    loaded = store.load_order("sig-abcdef12:ENTRY:2")
    assert loaded is not None
    assert loaded.broker_order_id == "G-2" and loaded.status is OrderStatus.ACKNOWLEDGED
    assert loaded.intent.attempt == 2 and loaded.intent.leg is OrderLeg.ENTRY
    assert loaded.mode == "live" and loaded.intent.price == 150.0
    assert store.load_order("nope:ENTRY:1") is None


def test_a_signal_id_with_colons_still_loads(tmp_path: Path) -> None:
    store = TradeStore(tmp_path / "t.db")
    intent = OrderIntent(
        signal_id="a:b", leg=OrderLeg.PROTECT, trading_symbol="X", side="SELL", quantity=1,
        order_type="SL", price=1.0, trigger_price=2.0,
    )
    rec = OrderRecord(intent=intent, mode="dry_run", created_at=NOW, updated_at=NOW)
    store.save_order(rec)
    loaded = store.load_order(intent.idempotency_key)
    assert loaded is not None and loaded.intent.signal_id == "a:b"
    assert loaded.intent.trigger_price == 2.0
