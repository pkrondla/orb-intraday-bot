"""Phase 2 real-data check: print ORH/ORL and pivots so they can be compared with TradingView.

Usage:  python scripts/phase2_check_levels.py [YYYY-MM-DD]
Read-only. Uses the current-month Nifty futures contract and 3-minute candles.
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orb_intraday_bot.adapters.groww.factory import build_readonly_client  # noqa: E402
from orb_intraday_bot.adapters.groww.market_data import GrowwMarketData  # noqa: E402
from orb_intraday_bot.config import Settings  # noqa: E402
from orb_intraday_bot.config.settings import load_strategy_config  # noqa: E402
from orb_intraday_bot.core.clock import IST  # noqa: E402
from orb_intraday_bot.indicators.opening_range import OpeningRangeTracker  # noqa: E402
from orb_intraday_bot.indicators.pivots import pivots_from_session, session_ohlc  # noqa: E402
from orb_intraday_bot.indicators.zones import band_label, classify_zone  # noqa: E402
from orb_intraday_bot.runtime.guard import run_startup_checks  # noqa: E402


def main() -> int:
    settings = Settings()
    run_startup_checks(settings)
    cfg = load_strategy_config(ROOT / settings.strategy_config_path)
    md = GrowwMarketData(build_readonly_client(settings), cfg.instrument)

    today = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else datetime.now(IST).date()
    contract = md.futures_contract(today)
    print(f"Contract: {contract.trading_symbol} (expiry {contract.expiry}, "
          f"lot {contract.lot_size})")

    prev_day, prev = md.previous_session(contract, today)
    h, low, c = session_ohlc(prev)
    piv = pivots_from_session(prev)
    print(f"\nPrevious session {prev_day}: {len(prev)} candles  H={h} L={low} C={c}")
    for name, value in reversed(piv.ordered()):
        print(f"  {name:<3} {value:>10.2f}")

    candles = md.session_candles(contract, today)
    print(f"\nToday {today}: {len(candles)} closed candles")
    if not candles:
        print("  (no candles yet)")
        return 0
    tracker = OpeningRangeTracker()
    for candle in candles:
        for ev in tracker.update(candle):
            print(f"  {ev.kind} = {ev.value} (fixed at close of {ev.candle_start:%H:%M} candle)")
    print(f"  ORH={tracker.orh}  ORL={tracker.orl}")
    last = candles[-1]
    print(f"  Last candle {last.start:%H:%M} close {last.close} -> zone "
          f"{classify_zone(last.close, piv).value}, band {band_label(last.close, piv)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
