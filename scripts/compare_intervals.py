"""Debug: compare Groww 3-minute candles with the aggregate of its 1-minute candles.

Usage: python scripts/compare_intervals.py YYYY-MM-DD HH:MM HH:MM
Read-only.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, time
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
    client = build_readonly_client(settings)
    md = GrowwMarketData(client, cfg.instrument)
    day = date.fromisoformat(sys.argv[1])
    lo, hi = sys.argv[2], sys.argv[3]
    contract = md.futures_contract(day)

    res = client.get_historical_candles(
        exchange=cfg.instrument.exchange,
        segment=cfg.instrument.segment,
        groww_symbol=contract.groww_symbol,
        start_time=f"{day} 09:15:00",
        end_time=f"{day} {hi}:59",
        candle_interval=client.CANDLE_INTERVAL_MIN_1,
    )
    one = []
    for row in res.get("candles", []):
        ts = datetime.fromisoformat(str(row[0]))
        one.append((ts, *[float(x) for x in row[1:5]]))
    print(f"{len(one)} one-minute candles")

    print("\n3-minute from API vs aggregate of 1-minute:")
    print(f"{'time':<6}{'api_hi':>10}{'agg_hi':>10}{'api_lo':>10}{'agg_lo':>10}"
          f"{'api_cl':>10}{'agg_cl':>10}")
    for c in md.session_candles(contract, day):
        t = c.start.strftime("%H:%M")
        if not (lo <= t <= hi):
            continue
        start = datetime.combine(day, time(c.start.hour, c.start.minute))
        group = [r for r in one
                 if 0 <= (r[0].replace(tzinfo=None) - start).total_seconds() < 180]
        if not group:
            print(t, "no 1-minute data")
            continue
        print(f"{t:<6}{c.high:>10.1f}{max(r[2] for r in group):>10.1f}"
              f"{c.low:>10.1f}{min(r[3] for r in group):>10.1f}"
              f"{c.close:>10.1f}{group[-1][4]:>10.1f}")
    print("\n1-minute detail 09:15-09:27:")
    for r in one:
        if r[0].strftime("%H:%M") <= hi and r[0].strftime("%H:%M") >= lo:
            print(r[0].strftime("%H:%M"), *[f"{x:.1f}" for x in r[1:]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
