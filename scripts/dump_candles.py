"""Print a day's 3-minute candles for a time window (debug helper).

Usage: python scripts/dump_candles.py YYYY-MM-DD HH:MM HH:MM
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orb_intraday_bot.adapters.groww.factory import build_readonly_client  # noqa: E402
from orb_intraday_bot.adapters.groww.market_data import GrowwMarketData  # noqa: E402
from orb_intraday_bot.config import Settings  # noqa: E402
from orb_intraday_bot.config.settings import load_strategy_config  # noqa: E402


def main() -> int:
    settings = Settings()
    cfg = load_strategy_config(ROOT / settings.strategy_config_path)
    md = GrowwMarketData(build_readonly_client(settings), cfg.instrument)
    day = date.fromisoformat(sys.argv[1])
    lo, hi = sys.argv[2], sys.argv[3]
    contract = md.futures_contract(day)
    print(f"{'time':<6}{'open':>10}{'high':>10}{'low':>10}{'close':>10}")
    for c in md.session_candles(contract, day):
        t = c.start.strftime("%H:%M")
        if lo <= t <= hi:
            print(f"{t:<6}{c.open:>10.1f}{c.high:>10.1f}{c.low:>10.1f}{c.close:>10.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
