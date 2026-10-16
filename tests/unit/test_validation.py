"""Phase 9: config overrides, T1 cap, break-even flag, history cache, multi-day replay, statistics,
sanity checks, live-versus-replay reconciliation and the variant sweep."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from orb_intraday_bot import cli
from orb_intraday_bot.analysis.backtest import (
    DayResult,
    SignalRecord,
    missing_candles,
    run_range,
    split_sessions,
)
from orb_intraday_bot.analysis.history import load_cache, load_history
from orb_intraday_bot.analysis.reconcile import format_reconciliation, reconcile
from orb_intraday_bot.analysis.stats import (
    check_results,
    data_quality,
    format_report,
    group_by,
    overall,
    rows_from_results,
)
from orb_intraday_bot.analysis.sweep import (
    DEFAULT_VARIANTS,
    Variant,
    format_sweep,
    parse_variant,
    run_sweep,
)
from orb_intraday_bot.config.settings import StrategyConfig, apply_overrides
from orb_intraday_bot.core.clock import IST
from orb_intraday_bot.core.models import Candle, Direction
from orb_intraday_bot.core.signals import Setup
from orb_intraday_bot.paper.models import ExitReason
from orb_intraday_bot.risk.targets import plan_targets
from orb_intraday_bot.storage.store import TradeStore
from tests.helpers import at, series
from tests.unit.test_paper import CFG, bar, make_trade
from tests.unit.test_retest_and_swing import real_day
from tests.unit.test_risk_and_engine import STOPS, WIDE, make
from tests.unit.test_runtime import PREV_DAY, prev_session

LOT = 65
DAY = date(2026, 10, 15)


def two_days() -> list[Candle]:
    """The previous session and the real 15 Oct morning (ends at 10:03, no 15:27 candle)."""
    return prev_session() + real_day()


# ---- config overrides --------------------------------------------------------------------
def test_apply_overrides_sets_nested_values() -> None:
    cfg = apply_overrides(
        StrategyConfig(),
        ["targets.t1_max_distance_points=75", "arbiter.max_trades_per_day=3",
         "enabled_setups.whipsaw_structure=false", "sizing.risk_basis=hard_stop"],
    )
    assert cfg.targets.t1_max_distance_points == 75
    assert cfg.arbiter.max_trades_per_day == 3
    assert cfg.enabled_setups.whipsaw_structure is False
    assert cfg.sizing.risk_basis == "hard_stop"
    assert StrategyConfig().targets.t1_max_distance_points is None       # the base is untouched


def test_apply_overrides_can_clear_a_value() -> None:
    cfg = apply_overrides(StrategyConfig(), ["targets.t1_max_distance_points=75"])
    cleared = apply_overrides(cfg, ["targets.t1_max_distance_points=null"])
    assert cleared.targets.t1_max_distance_points is None


@pytest.mark.parametrize(
    "bad",
    ["nonsense", "targets.nope=1", "nosuch.key=1", "stops.hard_stop_points=-5", "=3"],
)
def test_apply_overrides_rejects_bad_input(bad: str) -> None:
    with pytest.raises(ValueError):
        apply_overrides(StrategyConfig(), [bad])


# ---- T1 cap and the break-even flag ------------------------------------------------------
def test_t1_is_not_capped_by_default() -> None:
    sig = make(entry=121)             # WIDE: R2 140; T1 halfway 130.5
    assert plan_targets(sig, WIDE, 110.0, 95.0, STOPS).t1 == pytest.approx(130.5)


def test_t1_cap_pulls_a_far_t1_in() -> None:
    sig = make(entry=121)
    plan = plan_targets(sig, WIDE, 110.0, 95.0, STOPS, t1_max_points=5)
    assert plan.t1 == pytest.approx(126.0) and plan.t1_points == pytest.approx(5)
    assert "capped at 5 pts" in plan.t1_basis
    assert plan.t2 == 140


def test_t1_cap_leaves_a_near_t1_alone_and_mirrors_for_shorts() -> None:
    near = plan_targets(make(entry=121), WIDE, 110.0, 95.0, STOPS, t1_max_points=50)
    assert near.t1 == pytest.approx(130.5)
    short = plan_targets(
        make(direction=Direction.SHORT, entry=79, level=95, stop_level=95, target_ref="S2",
             level_name="ORL"),
        WIDE, 110.0, 95.0, STOPS, t1_max_points=4,
    )
    assert short.t1 == pytest.approx(75.0)


def run_t1(config: StrategyConfig) -> tuple[list[ExitReason], float]:
    from orb_intraday_bot.paper.engine import PaperEngine

    trade, plan = make_trade(Direction.LONG)
    engine = PaperEngine(config)
    from tests.unit.test_paper import OPENED

    position = engine.open(trade, plan, OPENED)
    assert position is not None
    engine.on_candle(Candle(at(9, 36), 22800, 22835, 22795, 22830))     # T1 22830 reached
    engine.on_candle(Candle(at(9, 39), 22825, 22826, 22760, 22765))      # falls back below entry
    return [f.reason for f in position.fills], position.active_stop


def test_break_even_stop_after_t1_by_default() -> None:
    reasons, _ = run_t1(CFG)
    assert reasons == [ExitReason.T1, ExitReason.BREAKEVEN_STOP]


def test_break_even_flag_off_keeps_the_original_stop() -> None:
    cfg = apply_overrides(CFG, ["targets.move_sl_to_entry_after_t1=false"])
    reasons, stop = run_t1(cfg)
    assert reasons == [ExitReason.T1, ExitReason.HARD_STOP] and stop == 22765.0


def test_unused_bar_helper_is_importable() -> None:
    assert bar(9, 36, 1, 2, 1, 2).is_candle        # keeps the shared helper import honest


# ---- history cache -----------------------------------------------------------------------
class Fetcher:
    def __init__(self, candles: list[Candle]) -> None:
        self.candles = candles
        self.calls: list[tuple[datetime, datetime]] = []

    def __call__(self, start: datetime, end: datetime) -> list[Candle]:
        self.calls.append((start, end))
        return [c for c in self.candles if start <= c.start < end]


def test_history_is_fetched_once_then_served_from_cache(tmp_path: Path) -> None:
    path = tmp_path / "h.json"
    fetch = Fetcher(two_days())
    today = date(2026, 10, 20)
    first = load_history(path, fetch, DAY, DAY, today)
    assert len(first.candles) == len(two_days()) and len(fetch.calls) == 1
    again = load_history(path, fetch, DAY, DAY, today)
    assert len(fetch.calls) == 1 and len(again.candles) == len(first.candles)
    assert len(load_cache(path).candles) == len(first.candles)


def test_refresh_and_today_always_refetch(tmp_path: Path) -> None:
    path = tmp_path / "h.json"
    fetch = Fetcher(two_days())
    load_history(path, fetch, DAY, DAY, date(2026, 10, 20))
    load_history(path, fetch, DAY, DAY, date(2026, 10, 20), refresh=True)
    assert len(fetch.calls) == 2
    load_history(path, fetch, DAY, DAY, DAY)             # "today" is never complete
    load_history(path, fetch, DAY, DAY, DAY)
    assert len(fetch.calls) == 4


def test_cache_merges_a_wider_range_and_prefers_fresh_candles(tmp_path: Path) -> None:
    path = tmp_path / "h.json"
    candles = two_days()
    load_history(path, Fetcher(candles), DAY, DAY, date(2026, 10, 20))
    changed = [
        Candle(c.start, c.open, c.high, c.low, c.close, 7.0) if c.start.date() == DAY else c
        for c in candles
    ]
    merged = load_history(
        path, Fetcher(changed), DAY, DAY + timedelta(days=1), date(2026, 10, 20), refresh=True
    )
    assert all(c.volume == 7.0 for c in merged.candles if c.start.date() == DAY)
    assert len(merged.candles) == len(candles)


def test_offline_returns_whatever_is_cached(tmp_path: Path) -> None:
    path = tmp_path / "h.json"
    assert load_history(path, None, DAY, DAY, date(2026, 10, 20)).candles == []


def test_corrupt_cache_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "h.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_cache(path).candles == []


# ---- multi-day replay --------------------------------------------------------------------
def replay(today: date | None = None, cfg: StrategyConfig = CFG) -> list[DayResult]:
    return run_range(cfg, two_days(), "NIFTY26OCTFUT", LOT, today=today)


def test_first_session_is_skipped_for_lack_of_pivots() -> None:
    results = replay(today=date(2026, 10, 20))
    assert [r.day for r in results] == [DAY] and results[0].previous_day == PREV_DAY


def test_open_positions_are_closed_when_the_last_candle_is_missing() -> None:
    res = replay(today=date(2026, 10, 20))[0]
    assert res.positions and all(not p.is_open for p in res.positions)
    assert any(f.reason is ExitReason.EOD for p in res.positions for f in p.fills)


def test_today_is_not_force_closed() -> None:
    res = replay(today=DAY)[0]
    assert any(p.is_open for p in res.positions)


def test_only_and_min_candles_filter_days() -> None:
    assert run_range(CFG, two_days(), "S", LOT, only={date(2030, 1, 1)}) == []
    assert run_range(CFG, two_days(), "S", LOT, min_candles=125) == []
    assert len(run_range(CFG, two_days(), "S", LOT, min_candles=10)) == 1


def test_split_sessions_and_missing_candles() -> None:
    sessions = split_sessions(two_days())
    assert list(sessions) == [PREV_DAY, DAY]
    day = sessions[DAY]
    assert missing_candles(day) == 0
    assert missing_candles(day[:3] + day[5:]) == 2


def test_replay_records_signals_with_their_fate() -> None:
    res = replay(today=date(2026, 10, 20))[0]
    assert {s.status for s in res.signals} <= {"accepted", "rejected", "released"}
    assert any(s.status == "accepted" and s.setup == Setup.ORB_BREAKOUT.value for s in res.signals)


# ---- statistics and sanity checks --------------------------------------------------------
def test_trade_rows_and_overall() -> None:
    results = replay(today=date(2026, 10, 20))
    rows = rows_from_results(results)
    assert len(rows) == len(results[0].positions) >= 1
    row = rows[0]
    assert row.risk_points == pytest.approx(35.0) or row.risk_points > 0
    assert row.r == pytest.approx(row.points / row.risk_points)
    o = overall(rows, days=1)
    assert o.trades == len(rows) and o.days_with_trades == 1
    assert o.group.points == pytest.approx(sum(r.points for r in rows), abs=0.2)
    assert o.max_drawdown_net >= 0 and o.losing_streak >= 0


def test_grouping_totals_match() -> None:
    rows = rows_from_results(replay(today=date(2026, 10, 20)))
    for key in (lambda r: r.setup, lambda r: r.direction, lambda r: r.exits):
        groups = group_by(rows, key)
        assert sum(g.trades for g in groups) == len(rows)
        assert sum(g.net for g in groups) == pytest.approx(sum(r.net for r in rows), abs=len(rows))


def test_report_text_has_every_section() -> None:
    res = replay(today=date(2026, 10, 20))
    text = format_report(rows_from_results(res), 1, [s for r in res for s in r.signals])
    for part in ("by setup", "by direction", "by zone", "by exits", "signals:", "profit factor"):
        assert part in text
    assert format_report([], 0) == "No closed trades."


def test_sanity_checks_pass_on_a_clean_replay() -> None:
    assert check_results(replay(today=date(2026, 10, 20)), CFG) == []


def test_sanity_checks_catch_problems() -> None:
    res = replay(today=DAY)                            # a position is still open
    assert any("still open" in p for p in check_results(res, CFG))
    capped = apply_overrides(CFG, ["arbiter.max_trades_per_day=1"])
    res = replay(today=date(2026, 10, 20))
    if len(res[0].positions) > 1:
        assert any("daily cap" in p for p in check_results(res, capped))
    res[0].positions.append(res[0].positions[0])
    assert any("duplicate" in p for p in check_results(res, CFG))


def test_data_quality_line() -> None:
    res = replay(today=date(2026, 10, 20))
    line = data_quality(res)
    assert line is not None and "missing candles" in line
    res[0].candle_count = 125
    assert data_quality(res) is None


# ---- reconciliation ----------------------------------------------------------------------
def test_identical_runs_reconcile_cleanly() -> None:
    res = replay(today=date(2026, 10, 20))[0]
    rec = reconcile(DAY, res.signals, res.positions, res)
    assert rec.clean and len(rec.matched) == len(res.positions)
    assert "agree" in format_reconciliation(rec)


def test_a_signal_the_live_run_missed_is_reported() -> None:
    res = replay(today=date(2026, 10, 20))[0]
    victim = next(s for s in res.signals if s.status == "accepted")
    live = [
        SignalRecord(s.signal_id, s.time, s.setup, s.level, s.direction,
                     "rejected", "stale", "the bot was not running")
        if s.signal_id == victim.signal_id else s
        for s in res.signals
    ]
    live_positions = [p for p in res.positions if p.position_id != victim.signal_id]
    rec = reconcile(DAY, live, live_positions, res)
    assert not rec.clean
    assert [s.signal_id for s, _ in rec.only_replay] == [victim.signal_id]
    text = format_reconciliation(rec)
    assert "REPLAY ONLY" in text and "stale" in text and "differences found" in text


def test_a_live_only_trade_and_different_exits_are_reported() -> None:
    res = replay(today=date(2026, 10, 20))[0]
    extra = SignalRecord("zzz", "10:00", "ORB_BREAKOUT", "ORL", "SHORT", "accepted", "", "")
    rec = reconcile(DAY, [*res.signals, extra], res.positions, res)
    assert [s.signal_id for s, _ in rec.only_live] == ["zzz"]
    assert "LIVE ONLY" in format_reconciliation(rec)

    # same trade, exits 20 points apart
    other = replay(today=date(2026, 10, 20))[0]
    first = other.positions[0]
    first.fills = [replace(f, futures_points=f.futures_points + 20) for f in first.fills]
    rec2 = reconcile(DAY, res.signals, other.positions, res)
    assert any(m.differs for m in rec2.matched) and "exits differ" in format_reconciliation(rec2)


# ---- sweep -------------------------------------------------------------------------------
def test_parse_variant() -> None:
    v = parse_variant("tight|targets.t1_max_distance_points=50, arbiter.max_trades_per_day=3")
    assert v.name == "tight"
    assert v.overrides == ("targets.t1_max_distance_points=50", "arbiter.max_trades_per_day=3")
    assert parse_variant("plain") == Variant("plain", ())


def test_default_grid_is_valid_and_starts_from_the_baseline() -> None:
    assert DEFAULT_VARIANTS[0].name == "baseline"
    for variant in DEFAULT_VARIANTS:
        apply_overrides(StrategyConfig(), variant.overrides)       # every override is real
    assert len({v.name for v in DEFAULT_VARIANTS}) == len(DEFAULT_VARIANTS)


def test_sweep_runs_variants_on_the_same_candles() -> None:
    variants = [
        Variant("baseline"),
        Variant("one trade", ("arbiter.max_trades_per_day=1",)),
        Variant("no structure", ("enabled_setups.whipsaw_structure=false",)),
    ]
    results = run_sweep(
        CFG, variants, two_days(), "S", LOT, None, None, None, date(2026, 10, 20), 0
    )
    assert [r.variant.name for r in results] == ["baseline", "one trade", "no structure"]
    assert results[1].stats.trades <= 1 <= results[0].stats.trades
    assert results[0].stats.group.points == pytest.approx(
        sum(r.points for r in rows_from_results(results[0].results))
    )
    table = format_sweep(results)
    assert "baseline" in table and "one trade" in table and "d pts" in table


# ---- store and CLI -----------------------------------------------------------------------
def test_store_lists_trade_days() -> None:
    store = TradeStore(":memory:")
    assert store.trade_days() == []
    from tests.unit.test_runtime import make_day

    day = make_day(store)
    for candle in real_day():
        day.on_candle(candle)
    assert store.trade_days() == [DAY]


def test_new_commands_parse() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["replay", "--last", "30", "--set", "a.b=1", "--set", "c.d=2"])
    assert args.last == 30 and args.set == ["a.b=1", "c.d=2"]
    args = parser.parse_args(["replay", "--from", "2026-09-01", "--to", "2026-09-30", "--csv", "x"])
    assert (args.first, args.to, args.csv) == ("2026-09-01", "2026-09-30", "x")
    swept = parser.parse_args(["sweep", "--last", "20", "--variant", "a|x.y=1"])
    assert swept.variant == ["a|x.y=1"]
    assert parser.parse_args(["report", "--from", "2026-10-01"]).first == "2026-10-01"
    assert parser.parse_args(["reconcile", "2026-10-15"]).date == "2026-10-15"


def test_report_command_on_an_empty_and_a_filled_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.unit.test_runtime import make_day

    monkeypatch.setattr(cli, "_load", lambda: (None, CFG, tmp_path))
    assert cli.main(["report"]) == 1
    store = TradeStore(tmp_path / CFG.paper.database_file)
    day = make_day(store)
    for candle in real_day():
        day.on_candle(candle)
    day.paper.close_all(at(10, 3), 22890.0)
    store.close()
    assert cli.main(["report"]) == 0
    out = capsys.readouterr().out
    assert "Forward test: 1 day" in out and "by setup" in out


def test_candles_helper_series_uses_ist() -> None:
    assert series([(1, 2, 1, 2)])[0].start.tzinfo is not None and at(9, 15).tzinfo == IST
