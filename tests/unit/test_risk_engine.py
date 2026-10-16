"""Risk engine gates and arming decisions."""

from __future__ import annotations

from datetime import date

from pydantic import SecretStr

from orb_intraday_bot.config.settings import Settings
from orb_intraday_bot.risk.engine import RiskEngine
from orb_intraday_bot.risk.limits import RiskLimits
from orb_intraday_bot.runtime.arming import resolve_mode


def test_entry_denied_when_over_lot_cap() -> None:
    eng = RiskEngine(limits=RiskLimits(max_lots_per_trade=1), capital_inr=500_000)
    eng.reset_day(date(2026, 10, 16))
    assert eng.check_entry(lots=2).allowed is False


def test_daily_loss_trips_kill_switch() -> None:
    eng = RiskEngine(
        limits=RiskLimits(daily_loss_limit_pct=0.01, loss_streak_halt=9),
        capital_inr=500_000,
    )
    eng.reset_day(date(2026, 10, 16))
    eng.record_open()
    eng.record_close(-6_000)   # over 5_000 limit
    assert eng.state.halted
    assert eng.check_entry(lots=1).allowed is False


def test_loss_streak_trips() -> None:
    eng = RiskEngine(
        limits=RiskLimits(daily_loss_limit_pct=0.5, loss_streak_halt=2),
        capital_inr=500_000,
    )
    eng.reset_day(date(2026, 10, 16))
    eng.record_open()
    eng.record_close(-100)
    eng.record_open()
    eng.record_close(-100)
    assert eng.state.halted
    assert "loss streak" in eng.state.halt_reason


def test_arming_requires_all_gates() -> None:
    base = Settings.model_construct(
        live_mode="live",
        live_armed=True,
        groww_trading_totp_token=SecretStr("tok"),
        groww_trading_totp_secret=SecretStr("sec"),
    )
    assert resolve_mode(base, trading_credentials_present=True).is_live
    assert not resolve_mode(base, trading_credentials_present=False).is_live
    disarmed = Settings.model_construct(
        live_mode="live",
        live_armed=False,
        groww_trading_totp_token=SecretStr("tok"),
        groww_trading_totp_secret=SecretStr("sec"),
    )
    d = resolve_mode(disarmed, trading_credentials_present=True)
    assert d.mode == "paper"
    assert resolve_mode(
        Settings.model_construct(live_mode="shadow", live_armed=False),
        trading_credentials_present=False,
    ).mode == "shadow"
