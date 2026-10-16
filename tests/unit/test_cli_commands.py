"""The command line end to end: real parser, real services, fake market data, temp folder.

The market is `GoldenMarket`: the recorded 54-session candle file served like the Groww adapter
would serve it. Nothing here touches the network, `.env` or the real data folder.
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from orb_intraday_bot import cli
from orb_intraday_bot.analysis.backtest import split_sessions
from orb_intraday_bot.app.context import AppContext, load_context
from orb_intraday_bot.app.replay import ReplayRequest, requested_days
from orb_intraday_bot.config.settings import load_strategy_config
from orb_intraday_bot.core.clock import FixedClock
from orb_intraday_bot.core.models import Candle
from orb_intraday_bot.data.contract import FuturesContract
from orb_intraday_bot.indicators.pivots import pivots_from_session
from orb_intraday_bot.options.planner import OptionPlanner
from orb_intraday_bot.runtime.day import TradingDay
from orb_intraday_bot.runtime.synthetic import SyntheticOptions
from orb_intraday_bot.storage.store import TradeStore
from tests.golden.snapshot import load_candles
from tests.runtime_support import CFG, CONTRACT, LOT, FakeSource, at

TODAY = date(2026, 10, 20)
RANGE = ["--from", "2026-10-05", "--to", "2026-10-15"]


class GoldenMarket(FakeSource):
    """FakeSource plus the history fetch used by replay, sweep and reconcile."""

    def __init__(self, candles: list[Candle]) -> None:
        super().__init__(FixedClock(at(10, 0)), candles)
        self.price_override = (at(0, 0), 22800.0)       # a futures price the fake chain covers

    def fetch_candles(
        self, contract: FuturesContract, start: datetime, end: datetime
    ) -> list[Candle]:
        return [c for c in self.candles if start <= c.start < end]


@pytest.fixture(scope="module")
def golden_candles() -> list[Candle]:
    return load_candles()


@pytest.fixture
def market(
    monkeypatch: pytest.MonkeyPatch,
    golden_candles: list[Candle],
    cli_context: Callable[..., AppContext],
) -> GoldenMarket:
    cli_context()
    fake = GoldenMarket(golden_candles)
    monkeypatch.setattr(cli, "build_market_data", lambda ctx: fake)
    monkeypatch.setattr(cli, "_today", lambda: TODAY)
    return fake


# ---- choosing the days -------------------------------------------------------------------
def test_requested_days_for_each_way_of_asking() -> None:
    today = date(2026, 10, 20)
    exact = requested_days(ReplayRequest(dates=("2026-10-14", "2026-10-12")), today)
    assert exact == (date(2026, 10, 12), date(2026, 10, 14),
                     {date(2026, 10, 12), date(2026, 10, 14)})
    assert requested_days(ReplayRequest(first="2026-10-01", to="2026-10-09"), today) == (
        date(2026, 10, 1), date(2026, 10, 9), None)
    assert requested_days(ReplayRequest(to="2026-10-09"), today)[0] == date(2026, 8, 10)
    assert requested_days(ReplayRequest(first="2026-10-01"), today) == (
        date(2026, 10, 1), today, None)
    assert requested_days(ReplayRequest(last=10), today) == (date(2026, 9, 20), today, None)
    assert requested_days(ReplayRequest(), today) == (today, today, {today})


# ---- replay ------------------------------------------------------------------------------
def test_replay_prints_the_report_and_writes_the_csv(
    market: GoldenMarket, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_csv = tmp_path / "trades.csv"
    code = cli.main(["replay", *RANGE, "--trades", "--csv", str(out_csv)])
    out = capsys.readouterr().out
    assert code == 0
    assert "Sanity checks: all passed." in out
    assert "2026-10-15" in out and "by setup" in out
    assert f"Trades written to {out_csv}" in out
    rows = list(csv.reader(out_csv.open(encoding="utf-8")))
    assert rows[0][:3] == ["day", "time", "setup"] and len(rows) > 1
    assert (tmp_path / "history" / f"{CONTRACT.trading_symbol}.json").exists()


def test_replay_events_and_overrides_are_echoed(
    market: GoldenMarket, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(
        ["replay", "2026-10-14", "2026-10-15", "--events", "--set", "stops.hard_stop_points=30"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "config overrides: stops.hard_stop_points=30" in out
    assert "=== 2026-10-15" in out


def test_replay_candles_mode_prints_every_candle(
    market: GoldenMarket, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["replay", "2026-10-15", "--candles"]) == 0
    assert "09:33" in capsys.readouterr().out


def test_replay_with_no_sessions_says_so(
    market: GoldenMarket, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["replay", "2026-01-05"]) == 1
    assert "No sessions to replay" in capsys.readouterr().out


def test_replay_last_n_uses_the_most_recent_sessions(
    market: GoldenMarket, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["replay", "--last", "3", "--to", "2026-10-15"])
    assert code == 0
    sessions = [d for d in split_sessions(market.candles) if d <= date(2026, 10, 15)]
    out = capsys.readouterr().out
    assert str(sessions[-1]) in out


# ---- sweep -------------------------------------------------------------------------------
def test_sweep_compares_variants(
    market: GoldenMarket, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main([
        "sweep", *RANGE, "--set", "paper.slippage_option_points=0",
        "--variant", "tight|stops.hard_stop_points=30",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "baseline" in out and "tight" in out
    assert "base config overrides" in out and "Replay prices are synthetic" in out


# ---- reconcile ---------------------------------------------------------------------------
def _record_live_day(ctx: AppContext, candles: list[Candle], day: date) -> None:
    """Run the same code the live runner runs, into the on-disk database."""
    sessions = split_sessions(candles)
    earlier = [d for d in sessions if d < day]
    store = TradeStore(ctx.database_path)
    trading = TradingDay(
        ctx.cfg, day, CONTRACT.trading_symbol, pivots_from_session(sessions[earlier[-1]]),
        OptionPlanner(ctx.cfg, SyntheticOptions(LOT)), store,
    )
    for candle in sessions[day]:
        trading.on_candle(candle)
    if trading.paper.open_positions:
        last = sessions[day][-1]
        trading.paper.close_all(last.start + timedelta(minutes=3), last.close)
    store.close()


def test_reconcile_without_a_database_or_without_that_day(
    market: GoldenMarket, cli_context: Callable[..., AppContext],
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = cli_context()
    assert cli.main(["reconcile", "2026-10-15"]) == 1
    assert "No database yet" in capsys.readouterr().out
    TradeStore(ctx.database_path).close()
    assert cli.main(["reconcile", "2026-10-15"]) == 1
    assert "nothing to compare" in capsys.readouterr().out
    assert cli.main(["reconcile", "2026-01-05"]) == 1
    assert "No candles for 2026-01-05" in capsys.readouterr().out


def test_reconcile_finds_a_live_day_identical_to_its_replay(
    market: GoldenMarket, cli_context: Callable[..., AppContext],
    golden_candles: list[Candle], capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = cli_context()
    _record_live_day(ctx, golden_candles, date(2026, 10, 15))
    code = cli.main(["reconcile", "2026-10-15"])
    out = capsys.readouterr().out
    assert code == 0
    assert "replay used" in out
    assert "0 only in the replay, 0 only live" in out
    assert "the live run and the replay agree" in out


# ---- report and summary on a recorded day ------------------------------------------------
def test_report_and_summary_read_the_recorded_day(
    market: GoldenMarket, cli_context: Callable[..., AppContext],
    golden_candles: list[Candle], capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = cli_context()
    _record_live_day(ctx, golden_candles, date(2026, 10, 15))
    assert cli.main(["report", "--from", "2026-10-01", "--to", "2026-10-31"]) == 0
    assert "Forward test: 1 day" in capsys.readouterr().out
    assert cli.main(["report", "--from", "2027-01-01"]) == 0
    assert "Forward test: 0 day" in capsys.readouterr().out
    assert cli.main(["summary", "2026-10-15"]) == 0
    assert "2026-10-15" in capsys.readouterr().out


# ---- check -------------------------------------------------------------------------------
def test_check_passes_with_a_working_market(
    market: GoldenMarket, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["check"])
    out = capsys.readouterr().out
    assert code == 0
    assert "OK    login and contract" in out and "OK    option chain and strike pick" in out
    assert "telegram: not configured" in out and "All checks passed." in out


def test_check_reports_a_failing_step_and_stops_if_login_fails(
    market: GoldenMarket, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_chain(expiry: date) -> object:
        raise ConnectionError("chain down")

    monkeypatch.setattr(market, "option_chain", no_chain)
    assert cli.main(["check"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  option chain and strike pick: ConnectionError: chain down" in out
    assert "1 check(s) failed." in out

    def no_login(today: date | None = None) -> object:
        raise PermissionError("bad credentials")

    monkeypatch.setattr(market, "futures_contract", no_login)
    assert cli.main(["check"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  login and contract: PermissionError" in out
    assert "previous session" not in out


# ---- context and the safety gate ---------------------------------------------------------
def test_load_context_refuses_real_orders_outside_live_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("LIVE_MODE", "paper")
    with pytest.raises(Exception, match="DRY_RUN=false needs LIVE_MODE=live"):
        load_context()


def test_load_context_reads_the_config_and_resolves_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    ctx = load_context()
    assert ctx.data_dir == tmp_path
    assert ctx.cfg.instrument.underlying
    assert ctx.database_path == tmp_path / ctx.cfg.paper.database_file
    assert ctx.history_path("X") == tmp_path / "history" / "X.json"


def test_the_default_config_file_equals_the_built_in_defaults() -> None:
    """Tests build `StrategyConfig()`; the golden replay loads the YAML. They must agree."""
    from_file = load_strategy_config(Path(__file__).parents[2] / "config" / "default.yaml")
    assert CFG.model_dump() == from_file.model_dump()
