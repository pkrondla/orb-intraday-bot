"""/capital, /riskpct, /dailyloss, /maxlots, /limits: change the money limits without a redeploy."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from orb_intraday_bot.app.context import AppContext
from orb_intraday_bot.app.serve import ServeState, serve_loop, stop_file_path
from orb_intraday_bot.config.settings import Settings, StrategyConfig
from orb_intraday_bot.core.clock import IST, FixedClock
from orb_intraday_bot.risk.engine import RiskEngine
from orb_intraday_bot.risk.limits import RiskLimits
from orb_intraday_bot.risk.sizing import risk_budget_inr
from orb_intraday_bot.runtime.limit_overrides import (
    OVERRIDE_FILE_NAME,
    LimitControls,
    parse_number,
)
from orb_intraday_bot.runtime.live import RunOutcome
from orb_intraday_bot.runtime.telegram_commands import CommandKind, parse_command
from tests.runtime_support import Recorder
from tests.unit.test_operator_commands import _send
from tests.unit.test_serve_execution import _settings


def _fresh(tmp_path: Path, **settings: object) -> tuple[AppContext, ServeState]:
    """A context with its own config (the commands change it in place) and a risk engine."""
    cfg = StrategyConfig()
    ctx = AppContext(_settings(data_dir=tmp_path, **settings), cfg, tmp_path)
    risk = RiskEngine(limits=RiskLimits(), capital_inr=cfg.sizing.capital_inr)
    risk.reset_day(date(2026, 10, 16))
    return ctx, ServeState(bus=_bus(), risk=risk)


def _bus():  # noqa: ANN202
    from orb_intraday_bot.runtime.telegram_commands import CommandBus

    return CommandBus()


# ---- reading numbers ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("750000", 750000.0),
        ("7,50,000", 750000.0),
        ("Rs 7.5L", 750000.0),
        ("₹10 lakh", 1_000_000.0),
        ("2cr", 20_000_000.0),
        ("50k", 50_000.0),
        ("1.5%", 1.5),
        ("2", 2.0),
    ],
)
def test_numbers_are_read_the_way_people_type_them(text: str, value: float) -> None:
    assert parse_number(text) == value


@pytest.mark.parametrize("text", ["", "abc", "nan", "inf", "1.2.3", "off"])
def test_text_that_is_not_a_number_is_refused(text: str) -> None:
    assert parse_number(text) is None


def test_the_commands_are_parsed() -> None:
    for verb, kind in (
        ("capital", CommandKind.CAPITAL),
        ("riskpct", CommandKind.RISKPCT),
        ("dailyloss", CommandKind.DAILYLOSS),
        ("maxlots", CommandKind.MAXLOTS),
        ("limits", CommandKind.LIMITS),
    ):
        command = parse_command(f"/{verb} 5")
        assert (command.kind, command.arg) == (kind, "5")


# ---- the controls ------------------------------------------------------------------------
def test_a_change_reaches_the_live_config_and_is_saved(tmp_path: Path) -> None:
    cfg, risk = StrategyConfig(), RiskEngine(limits=RiskLimits())
    controls = LimitControls(cfg, risk, tmp_path)
    controls.set("capital", 1_000_000)
    controls.set("risk_pct", 2)
    controls.set("daily_loss_pct", 1.5)
    controls.set("max_lots", 3)
    assert risk_budget_inr(cfg.sizing) == pytest.approx(20_000)       # what the sizing now uses
    assert risk.capital_inr == 1_000_000
    assert risk.limits.daily_loss_limit_inr(risk.capital_inr) == pytest.approx(15_000)
    assert risk.limits.max_lots_per_trade == 3
    assert controls.risk_budget_inr() == pytest.approx(20_000)
    assert json.loads((tmp_path / OVERRIDE_FILE_NAME).read_text()) == {
        "capital": 1_000_000, "risk_pct": 2, "daily_loss_pct": 1.5, "max_lots": 3,
    }


def test_a_restart_brings_the_saved_values_back(tmp_path: Path) -> None:
    first = LimitControls(StrategyConfig(), RiskEngine(limits=RiskLimits()), tmp_path)
    first.set("capital", 750_000)
    first.set("max_lots", 2)
    cfg, risk = StrategyConfig(), RiskEngine(limits=RiskLimits())
    second = LimitControls(cfg, risk, tmp_path)
    assert second.load_saved() == []
    assert cfg.sizing.capital_inr == 750_000 and risk.capital_inr == 750_000
    assert risk.limits.max_lots_per_trade == 2
    assert second.default("capital") == 500_000                       # the default is kept apart
    assert second.is_set_by_operator("capital") and not second.is_set_by_operator("risk_pct")
    assert any("Capital Rs 750,000 set by Telegram" in n for n in second.startup_notes())


def test_values_outside_the_range_are_refused(tmp_path: Path) -> None:
    controls = LimitControls(StrategyConfig(), RiskEngine(limits=RiskLimits()), tmp_path)
    for key, value in (
        ("capital", 500), ("capital", 5_000_000_000), ("risk_pct", 0), ("risk_pct", 50),
        ("daily_loss_pct", 30), ("max_lots", 0), ("max_lots", 500), ("max_lots", 1.5),
    ):
        with pytest.raises(ValueError, match="must be"):
            controls.set(key, value)
    assert not (tmp_path / OVERRIDE_FILE_NAME).exists()
    assert controls.get("capital") == 500_000


def test_reset_goes_back_to_the_default(tmp_path: Path) -> None:
    cfg, risk = StrategyConfig(), RiskEngine(limits=RiskLimits())
    controls = LimitControls(cfg, risk, tmp_path)
    controls.set("capital", 900_000)
    controls.set("risk_pct", 2)
    controls.reset("capital")
    assert cfg.sizing.capital_inr == 500_000 and risk.capital_inr == 500_000
    assert cfg.sizing.risk_pct_per_trade == pytest.approx(0.02)       # only that one was reset
    controls.reset()
    assert cfg.sizing.risk_pct_per_trade == pytest.approx(0.01)
    assert not (tmp_path / OVERRIDE_FILE_NAME).exists()


def test_the_github_variables_are_the_default_and_bad_ones_are_refused(tmp_path: Path) -> None:
    cfg, risk = StrategyConfig(), RiskEngine(limits=RiskLimits())
    controls = LimitControls(cfg, risk, tmp_path)
    problems = controls.apply_configured(
        _settings(capital_inr=800_000.0, risk_per_trade_pct=75.0, max_lots_per_trade=2)
    )
    assert cfg.sizing.capital_inr == 800_000 and risk.limits.max_lots_per_trade == 2
    assert cfg.sizing.risk_pct_per_trade == pytest.approx(0.01)       # 75% refused
    assert len(problems) == 1 and "RISK_PER_TRADE_PCT=75.0 ignored" in problems[0]
    assert controls.default("capital") == 800_000                     # the new default
    controls.set("capital", 900_000)
    controls.reset("capital")
    assert cfg.sizing.capital_inr == 800_000                          # back to the variable


def test_an_unreadable_or_invalid_saved_file_leaves_the_defaults(tmp_path: Path) -> None:
    path = tmp_path / OVERRIDE_FILE_NAME
    path.write_text("not json", encoding="utf-8")
    cfg = StrategyConfig()
    controls = LimitControls(cfg, RiskEngine(limits=RiskLimits()), tmp_path)
    assert "could not be read" in controls.load_saved()[0]
    path.write_text(json.dumps({"capital": 5, "max_lots": 2, "other": 1}), encoding="utf-8")
    problems = controls.load_saved()
    assert len(problems) == 1 and "/capital ignored" in problems[0]
    assert cfg.sizing.capital_inr == 500_000


def test_empty_github_variables_mean_unset() -> None:
    settings = Settings(
        _env_file=None, CAPITAL_INR="", RISK_PER_TRADE_PCT=" ", MAX_LOTS_PER_TRADE="3",
    )  # type: ignore[call-arg]
    assert settings.capital_inr is None and settings.risk_per_trade_pct is None
    assert settings.max_lots_per_trade == 3
    assert Settings(_env_file=None, CAPITAL_INR="750000").capital_inr == 750_000  # type: ignore[call-arg]


def test_lowering_the_loss_limit_below_todays_loss_trips_the_kill_switch(
    tmp_path: Path,
) -> None:
    cfg, risk = StrategyConfig(), RiskEngine(limits=RiskLimits())
    risk.reset_day(date(2026, 10, 16))
    risk.record_open()
    risk.record_close(-3_000)                                         # a loss of Rs 3,000 so far
    assert not risk.state.halted
    LimitControls(cfg, risk, tmp_path).set("daily_loss_pct", 0.5)     # limit is now Rs 2,500
    assert risk.state.halted and "daily loss limit" in risk.state.halt_reason


# ---- the Telegram commands ---------------------------------------------------------------
def test_capital_changes_the_sizing_and_says_what_it_means(tmp_path: Path) -> None:
    ctx, state = _fresh(tmp_path)
    reply = _send(CommandKind.CAPITAL, "7.5L", ctx=ctx, state=state)[0]
    assert "Capital: Rs 500,000 -> Rs 750,000" in reply
    assert "Risk per trade 1% = Rs 7,500" in reply and "daily loss limit 1% = Rs 7,500" in reply
    assert "kept across restarts and deploys" in reply
    assert ctx.cfg.sizing.capital_inr == 750_000 and state.risk.capital_inr == 750_000


def test_riskpct_dailyloss_and_maxlots_each_change_their_own_limit(tmp_path: Path) -> None:
    ctx, state = _fresh(tmp_path)
    assert "Risk per trade: 1% -> 1.5%" in _send(
        CommandKind.RISKPCT, "1.5%", ctx=ctx, state=state
    )[0]
    assert "Daily loss limit: 1% -> 2%" in _send(
        CommandKind.DAILYLOSS, "2", ctx=ctx, state=state
    )[0]
    assert "Max lots per trade: 1 -> 3" in _send(
        CommandKind.MAXLOTS, "3", ctx=ctx, state=state
    )[0]
    assert risk_budget_inr(ctx.cfg.sizing) == pytest.approx(7_500)
    assert state.risk.limits.daily_loss_limit_pct == pytest.approx(0.02)
    assert state.risk.limits.max_lots_per_trade == 3


def test_a_risk_per_trade_above_the_daily_limit_gets_a_warning(tmp_path: Path) -> None:
    ctx, state = _fresh(tmp_path)
    reply = _send(CommandKind.RISKPCT, "2", ctx=ctx, state=state)[0]      # daily limit is 1%
    assert "Warning: one losing trade can use the whole daily loss limit" in reply


def test_bad_input_changes_nothing_and_says_how_to_do_it(tmp_path: Path) -> None:
    ctx, state = _fresh(tmp_path)
    assert "not a number" in _send(CommandKind.CAPITAL, "lots", ctx=ctx, state=state)[0]
    refused = _send(CommandKind.CAPITAL, "50", ctx=ctx, state=state)[0]
    assert "must be between Rs 10,000 and Rs 100,000,000" in refused
    assert "Nothing changed" in refused
    assert "whole number" in _send(CommandKind.MAXLOTS, "1.5", ctx=ctx, state=state)[0]
    assert ctx.cfg.sizing.capital_inr == 500_000
    assert not (tmp_path / OVERRIDE_FILE_NAME).exists()


def test_limits_shows_every_value_and_where_it_comes_from(tmp_path: Path) -> None:
    ctx, state = _fresh(tmp_path)
    plain = _send(CommandKind.LIMITS, "", ctx=ctx, state=state)[0]
    assert "Capital: Rs 500,000 (default" in plain
    assert "Risk per trade: 1% = Rs 5,000 per trade (default" in plain
    assert "Daily loss limit: 1% = Rs 5,000 a day (default" in plain
    assert "Max lots per trade: 1 (default" in plain
    _send(CommandKind.CAPITAL, "600000", ctx=ctx, state=state)
    after = _send(CommandKind.LIMITS, "", ctx=ctx, state=state)[0]
    assert "Capital: Rs 600,000 (set by Telegram" in after


def test_reset_puts_a_limit_or_all_of_them_back(tmp_path: Path) -> None:
    ctx, state = _fresh(tmp_path)
    _send(CommandKind.CAPITAL, "600000", ctx=ctx, state=state)
    _send(CommandKind.MAXLOTS, "4", ctx=ctx, state=state)
    one = _send(CommandKind.CAPITAL, "reset", ctx=ctx, state=state)[0]
    assert "Rs 600,000 -> Rs 500,000" in one and "Back to the default" in one
    assert state.risk.limits.max_lots_per_trade == 4
    everything = _send(CommandKind.LIMITS, "reset", ctx=ctx, state=state)[0]
    assert "back to its default" in everything and state.risk.limits.max_lots_per_trade == 1
    assert not (tmp_path / OVERRIDE_FILE_NAME).exists()


def test_a_limit_change_leaves_an_audit_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ctx, state = _fresh(tmp_path)
    with caplog.at_level("INFO", logger="orb.serve"):
        _send(CommandKind.CAPITAL, "600000", ctx=ctx, state=state)
    events = [getattr(r, "event", "") for r in caplog.records]
    assert "operator_command" in events and "limit_changed" in events


def test_a_change_made_while_the_kill_switch_is_on_says_so(tmp_path: Path) -> None:
    ctx, state = _fresh(tmp_path)
    state.risk.trip("loss streak 2")
    reply = _send(CommandKind.MAXLOTS, "2", ctx=ctx, state=state)[0]
    assert "kill switch is on (loss streak 2)" in reply


# ---- at start ----------------------------------------------------------------------------
def test_the_service_applies_the_variables_then_the_saved_choice(tmp_path: Path) -> None:
    cfg = StrategyConfig()
    settings = Settings.model_construct(
        data_dir=tmp_path, telegram_bot_token=SecretStr(""), telegram_chat_id="",
        healthcheck_url="", live_mode="paper", live_armed=False,
        strategy_config_path=Path("config/default.yaml"),
        capital_inr=700_000.0, risk_per_trade_pct=None, daily_loss_limit_pct=None,
        max_lots_per_trade=None,
    )
    ctx = AppContext(settings, cfg, tmp_path)
    (tmp_path / OVERRIDE_FILE_NAME).write_text(json.dumps({"risk_pct": 2}), encoding="utf-8")
    notes = Recorder()
    seen: dict[str, float] = {}

    def session(c, n, s, cl, sl):  # noqa: ANN001
        seen["capital"] = s.risk.capital_inr
        seen["risk_pct"] = s.limits.get("risk_pct") if s.limits else 0.0
        stop_file_path(tmp_path).write_text("stop\n", encoding="utf-8")
        return RunOutcome(0, "ok")

    serve_loop(
        ctx, clock=FixedClock(datetime(2026, 10, 16, 9, 0, tzinfo=IST)),
        sleep=lambda _s: None, notifier=notes, session=session, max_sessions=1,
        idle_step_sec=1, holidays=frozenset(),
    )
    assert seen == {"capital": 700_000.0, "risk_pct": 2.0}
    assert any("ORB bot limits" in m and "Risk per trade 2% set by Telegram" in m
               for m in notes.messages)
