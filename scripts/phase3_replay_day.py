"""Real-data replay: run all setup detectors (Phases 3 and 4) over one or more days.

Usage:  python scripts/phase3_replay_day.py [YYYY-MM-DD ...] [--quiet]
Default day: today. `--quiet` prints only signals and a per-day summary.
Read-only. Prints pattern events and candidate signals (before zone filter / risk / options).
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orb_intraday_bot.adapters.groww.factory import build_readonly_client  # noqa: E402
from orb_intraday_bot.adapters.groww.market_data import GrowwMarketData  # noqa: E402
from orb_intraday_bot.config import Settings  # noqa: E402
from orb_intraday_bot.config.settings import load_strategy_config  # noqa: E402
from orb_intraday_bot.core.clock import IST  # noqa: E402
from orb_intraday_bot.engine.signal_engine import SignalEngine  # noqa: E402
from orb_intraday_bot.indicators.opening_range import OpeningRangeTracker  # noqa: E402
from orb_intraday_bot.patterns.detector import SetupDetector  # noqa: E402
from orb_intraday_bot.runtime.guard import run_startup_checks  # noqa: E402


def replay(md: GrowwMarketData, cfg, day: date, quiet: bool) -> Counter[str]:  # type: ignore[no-untyped-def]
    contract = md.futures_contract(day)
    pivots = md.previous_session_levels(contract, day).pivots()
    candles = md.session_candles(contract, day)
    print(f"\n=== {day} {contract.trading_symbol}: {len(candles)} candles; "
          f"R2={pivots.r2:.1f} S2={pivots.s2:.1f}")

    tracker = OpeningRangeTracker()
    detector = SetupDetector(cfg, pivots)
    engine = SignalEngine(cfg, pivots)
    counts: Counter[str] = Counter()
    for candle in candles:
        for lv in tracker.update(candle):
            if not quiet:
                print(f"{candle.start:%H:%M}  LEVEL {lv.kind} = {lv.value}")
        step = detector.update(candle, tracker.orh, tracker.orl)
        if not quiet:
            for ev in step.events:
                print(f"{candle.start:%H:%M}  {ev.kind.value:<18} {ev.level_name:<10} "
                      f"{ev.direction.value:<5} {ev.detail}")
        for s in step.signals:
            counts[f"signal {s.setup.value}"] += 1
        # No positions are simulated here (Phase 7), so open_directions stays empty.
        decision = engine.evaluate(candle, step.signals, tracker.orh, tracker.orl)
        for t in decision.accepted:
            s = t.signal
            counts["ACCEPTED"] += 1
            print(f"{candle.start:%H:%M}  ACCEPT {s.setup.value} {s.level_name} "
                  f"{s.direction.value} @ {s.entry_price:.1f}  zone={decision.zone.value}  "
                  f"stop {t.stop.stop_points:.1f} pts ({t.stop.basis})  "
                  f"T1 {t.targets.t1:.1f} ({t.targets.t1_basis})  "
                  f"T2 {t.targets.t2:.1f} ({t.targets.t2_basis})")
        for r in decision.rejected:
            s = r.signal
            counts[f"rejected:{r.stage}"] += 1
            print(f"{candle.start:%H:%M}  reject {s.setup.value} {s.level_name} "
                  f"{s.direction.value} @ {s.entry_price:.1f}  [{r.stage}] {r.reason}")
    print(f"trending day: {detector.trending.state}; {dict(counts) or 'no signals'}")
    return counts


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    quiet = "--quiet" in sys.argv
    settings = Settings()
    run_startup_checks(settings)
    cfg = load_strategy_config(ROOT / settings.strategy_config_path)
    md = GrowwMarketData(build_readonly_client(settings), cfg.instrument)
    days = [date.fromisoformat(a) for a in args] or [datetime.now(IST).date()]
    total: Counter[str] = Counter()
    for day in days:
        total += replay(md, cfg, day, quiet)
    if len(days) > 1:
        print(f"\nTOTAL over {len(days)} days: {dict(total)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
