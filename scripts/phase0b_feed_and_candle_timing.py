"""Phase 0b spike (run during market hours, 09:20-15:20 IST, takes about 5 minutes).

Answers the two questions left open by phase0_spike.py:
  A. Does the live feed give futures and option ticks, and how fast?
  B. How soon after a 3-minute boundary is the just-closed candle final?
     (compares the closed candle at +2s, +5s, +10s, +30s, +60s)

Read-only. Writes <DATA_DIR>/phase0b_report.json.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orb_intraday_bot.adapters.groww.factory import build_readonly_client, build_token_manager  # noqa: E402
from orb_intraday_bot.adapters.groww.feed import ReadOnlyGrowwFeed  # noqa: E402
from orb_intraday_bot.config import Settings  # noqa: E402
from orb_intraday_bot.core.clock import CANDLE_MINUTES, IST, candle_start  # noqa: E402
from orb_intraday_bot.runtime.guard import run_startup_checks  # noqa: E402

FMT = "%Y-%m-%d %H:%M:%S"
REPORT: dict[str, Any] = {}


def find_future(client: Any, today: str) -> dict[str, Any]:
    df = client.get_all_instruments()
    fut = df[(df["underlying_symbol"].astype(str).str.upper() == "NIFTY")
             & (df["instrument_type"].astype(str).str.upper() == "FUT")].copy()
    fut["expiry"] = fut["expiry_date"].astype(str).str[:10]
    return fut[fut["expiry"] >= today].sort_values("expiry").iloc[0].to_dict()  # type: ignore[no-any-return]


def find_atm_ce(client: Any, expiry: str, ref: float) -> dict[str, Any]:
    df = client.get_all_instruments()
    opt = df[(df["underlying_symbol"].astype(str).str.upper() == "NIFTY")
             & (df["instrument_type"].astype(str).str.upper() == "CE")].copy()
    opt = opt[opt["expiry_date"].astype(str).str[:10] == expiry]
    opt["dist"] = (opt["strike_price"].astype(float) - ref).abs()
    return opt.sort_values("dist").iloc[0].to_dict()  # type: ignore[no-any-return]


def feed_test(settings: Settings, fut: dict[str, Any], opt: dict[str, Any]) -> dict[str, Any]:
    feed = ReadOnlyGrowwFeed(build_token_manager(settings))
    callbacks = {"n": 0}
    instruments = [
        {"exchange": "NSE", "segment": "FNO", "exchange_token": str(fut["exchange_token"])},
        {"exchange": "NSE", "segment": "FNO", "exchange_token": str(opt["exchange_token"])},
    ]

    def on_data() -> None:
        callbacks["n"] += 1

    out: dict[str, Any] = {"instruments": instruments}
    try:
        out["subscribe_result"] = feed.subscribe_ltp(instruments, on_data_received=on_data)
    except Exception as exc:
        out["subscribe_error"] = f"{type(exc).__name__}: {exc}"
        return out

    threading.Thread(target=feed.consume, daemon=True).start()
    samples: list[Any] = []
    last = None
    changes = 0
    started = time.time()
    while time.time() - started < 30:
        time.sleep(1)
        snap = feed.get_ltp()
        samples.append(snap)
        if snap != last:
            changes += 1
            last = snap
    out.update({"callbacks_in_30s": callbacks["n"], "snapshot_changes_in_30s": changes,
                "first_sample": samples[0] if samples else None,
                "last_sample": samples[-1] if samples else None})
    return out


def rest_ltp_test(client: Any, fut: dict[str, Any], opt: dict[str, Any]) -> dict[str, Any]:
    symbols = f"NSE_{fut['trading_symbol']},NSE_{opt['trading_symbol']}"
    latencies: list[float] = []
    last: Any = None
    changes = 0
    for _ in range(20):
        t = time.time()
        last_new = client.get_ltp(exchange_trading_symbols=symbols, segment="FNO")
        latencies.append(time.time() - t)
        if last_new != last:
            changes += 1
            last = last_new
    latencies.sort()
    return {"symbols": symbols, "sample": last,
            "median_latency_s": round(latencies[len(latencies) // 2], 3),
            "max_latency_s": round(latencies[-1], 3), "value_changes_in_20_calls": changes}


def candle_timing(client: Any, symbol: str) -> dict[str, Any]:
    now = datetime.now(IST)
    boundary = candle_start(now) + timedelta(minutes=CANDLE_MINUTES)
    wait = (boundary - datetime.now(IST)).total_seconds()
    print(f"Waiting {wait:.0f}s for boundary {boundary.time()} ...")
    time.sleep(max(0.0, wait))
    closed_start = boundary - timedelta(minutes=CANDLE_MINUTES)

    def fetch() -> dict[str, Any]:
        end = datetime.now(IST)
        res = client.get_historical_candles(
            exchange="NSE", segment="FNO", groww_symbol=symbol,
            start_time=(boundary - timedelta(minutes=15)).strftime(FMT),
            end_time=end.strftime(FMT), candle_interval=client.CANDLE_INTERVAL_MIN_3)
        rows = {r[0]: r for r in res.get("candles", [])}
        key = closed_start.strftime("%Y-%m-%dT%H:%M:%S")
        new_key = boundary.strftime("%Y-%m-%dT%H:%M:%S")
        return {"closed_row": rows.get(key), "forming_row_present": new_key in rows,
                "at": datetime.now(IST).strftime("%H:%M:%S.%f")}

    results = {}
    t0 = boundary
    for delay in (2, 5, 10, 30, 60):
        sleep_for = (t0 + timedelta(seconds=delay) - datetime.now(IST)).total_seconds()
        time.sleep(max(0.0, sleep_for))
        results[f"+{delay}s"] = fetch()
        print(f"+{delay}s ->", results[f"+{delay}s"])
    final = results["+60s"]["closed_row"]
    stable_from = None
    for delay in (2, 5, 10, 30, 60):
        row = results[f"+{delay}s"]["closed_row"]
        if row is not None and final is not None and row[:6] == final[:6]:
            stable_from = delay
            break
    return {"boundary": boundary.isoformat(), "closed_candle_start": closed_start.isoformat(),
            "results": results, "final_matches_from_seconds": stable_from}


def main() -> int:
    settings = Settings()
    run_startup_checks(settings)
    client = build_readonly_client(settings)
    today = datetime.now(IST).date().isoformat()

    fut = find_future(client, today)
    ltp = client.get_ltp(exchange_trading_symbols=f"NSE_{fut['trading_symbol']}", segment="FNO")
    ref = float(next(iter(ltp.values())))
    wk = sorted(e for e in client.get_expiries(exchange="NSE", underlying_symbol="NIFTY",
                                               year=datetime.now(IST).year,
                                               month=datetime.now(IST).month)["expiries"]
                if e >= today)[0]
    opt = find_atm_ce(client, wk, ref)
    print(f"Future {fut['trading_symbol']} @ {ref}; option {opt['trading_symbol']} ({wk})")

    print("\n=== A1. REST LTP polling speed (20 calls, futures + option in one call) ===")
    REPORT["rest_ltp"] = rest_ltp_test(client, fut, opt)
    print(json.dumps(REPORT["rest_ltp"], default=str, indent=2))

    if "--with-feed" in sys.argv:
        print("\n=== A2. live feed (30 s) ===")
        try:
            REPORT["feed"] = feed_test(settings, fut, opt)
        except Exception as exc:
            REPORT["feed"] = {"error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(REPORT["feed"], default=str, indent=2)[:2000])

    print("\n=== B. candle timing ===")
    REPORT["candle_timing"] = candle_timing(client, str(fut["groww_symbol"]))
    print("closed candle final from:", REPORT["candle_timing"]["final_matches_from_seconds"], "s")

    out = settings.data_dir / "phase0b_report.json"
    out.write_text(json.dumps(REPORT, default=str, indent=2), encoding="utf-8")
    print(f"\nReport written to {out}")
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    # The feed's consume thread is a daemon, but force exit so nothing keeps the process alive.
    import os

    os._exit(code)
