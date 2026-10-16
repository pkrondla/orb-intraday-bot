"""Phase 0 spike: prove what the Groww API gives us, read-only.

Run (ideally during market hours, 09:20-15:25 IST, so candles and ticks are live):

    python scripts/phase0_spike.py

It checks, in order:
  1. Credentials and token (cached, with the persisted 429 cooldown)
  2. Current-month Nifty futures contract (expiry list, instruments file)
  3. 3-minute futures candles (response shape, last candle, publish delay)
  4. Futures LTP
  5. Nearest weekly option chain (is delta included?) and get_greeks
  6. Whether GrowwFeed can be imported (live ticks)

Everything is written in full to <DATA_DIR>/phase0_report.json. Nothing is ever ordered.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orb_intraday_bot.adapters.groww.factory import (  # noqa: E402
    MissingCredentialsError,
    build_readonly_client,
)
from orb_intraday_bot.config import Settings, load_strategy_config  # noqa: E402
from orb_intraday_bot.core.clock import IST  # noqa: E402
from orb_intraday_bot.runtime.guard import run_startup_checks  # noqa: E402

REPORT: dict[str, Any] = {}


def short(obj: Any, limit: int = 1500) -> str:
    text = json.dumps(obj, default=str, indent=2)
    return text if len(text) <= limit else text[:limit] + f"\n... ({len(text)} chars total)"


def step(name: str, fn: Any) -> Any:
    print(f"\n=== {name} ===")
    started = time.time()
    try:
        result = fn()
        REPORT[name] = {"ok": True, "seconds": round(time.time() - started, 2), "data": result}
        print(short(result))
        return result
    except Exception as exc:
        REPORT[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                        "trace": traceback.format_exc()}
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return None


def main() -> int:
    settings = Settings()
    run_startup_checks(settings)
    cfg = load_strategy_config(ROOT / settings.strategy_config_path)
    inst = cfg.instrument

    try:
        client = build_readonly_client(settings)
    except MissingCredentialsError as exc:
        print(f"{exc} See .env.example.")
        return 2
    today = datetime.now(IST).date()

    step("1_profile", lambda: client.get_user_profile())

    # -- futures contract --------------------------------------------------------------
    def find_future() -> dict[str, Any]:
        df = client.get_all_instruments()
        REPORT["instrument_columns"] = list(df.columns)
        und = df[df["underlying_symbol"].astype(str).str.upper() == inst.underlying]
        fut = und[und["instrument_type"].astype(str).str.upper() == "FUT"].copy()
        fut["expiry"] = fut["expiry_date"].astype(str).str[:10]
        fut = fut[fut["expiry"] >= today.isoformat()].sort_values("expiry")
        if fut.empty:
            raise RuntimeError("No NIFTY futures found in instruments file")
        row = fut.iloc[0].to_dict()
        row["all_future_expiries"] = fut["expiry"].tolist()
        return row

    fut = step("2_futures_contract", find_future)
    if not fut:
        return finish(1)

    groww_symbol = str(fut.get("groww_symbol"))
    trading_symbol = str(fut.get("trading_symbol"))

    step("2b_expiries_api", lambda: client.get_expiries(
        exchange=inst.exchange, underlying_symbol=inst.underlying,
        year=today.year, month=today.month))

    # -- candles -----------------------------------------------------------------------
    def candles() -> dict[str, Any]:
        end = datetime.now(IST)
        start = end - timedelta(days=3)
        fmt = "%Y-%m-%d %H:%M:%S"
        fetched_at = time.time()
        res = client.get_historical_candles(
            exchange=inst.exchange, segment=inst.segment, groww_symbol=groww_symbol,
            start_time=start.strftime(fmt), end_time=end.strftime(fmt),
            candle_interval=client.CANDLE_INTERVAL_MIN_3)
        return {"requested": [start.strftime(fmt), end.strftime(fmt)],
                "interval_constant": client.CANDLE_INTERVAL_MIN_3,
                "fetched_at_epoch": fetched_at, "response": res}

    cres = step("3_candles_3min", candles)
    if cres:
        payload = cres["response"]
        rows = payload.get("candles") if isinstance(payload, dict) else None
        if rows:
            print(f"\ncandle rows: {len(rows)}   first: {rows[0]}   last: {rows[-1]}")
            REPORT["candle_row_count"] = len(rows)
            REPORT["candle_last_row"] = rows[-1]
            REPORT["candle_first_row"] = rows[0]
        else:
            print("\nCould not find a 'candles' list; inspect phase0_report.json for the shape.")

    # -- LTP ---------------------------------------------------------------------------
    ltp = step("4_futures_ltp", lambda: client.get_ltp(
        exchange_trading_symbols=f"{inst.exchange}_{trading_symbol}",
        segment=inst.segment))

    # -- option chain ------------------------------------------------------------------
    def weekly_expiry() -> str:
        res = client.get_expiries(exchange=inst.exchange, underlying_symbol=inst.underlying,
                                  year=today.year, month=today.month)
        expiries = sorted(res.get("expiries", []) if isinstance(res, dict) else [])
        upcoming = [e for e in expiries if e >= today.isoformat()]
        if not upcoming:
            raise RuntimeError(f"No upcoming expiry in response: {res}")
        return str(upcoming[0])

    wk = step("5a_weekly_expiry", weekly_expiry)
    if wk:
        chain = step("5b_option_chain", lambda: client.get_option_chain(
            exchange=inst.exchange, underlying=inst.underlying, expiry_date=wk))
        REPORT["option_chain_has_delta"] = "delta" in json.dumps(chain, default=str).lower()
        print(f"\nOption chain contains 'delta': {REPORT['option_chain_has_delta']}")

        def one_option() -> dict[str, Any]:
            df = client.get_all_instruments()
            opt = df[(df["underlying_symbol"].astype(str).str.upper() == inst.underlying)
                     & (df["instrument_type"].astype(str).str.upper() == "CE")].copy()
            opt["expiry"] = opt["expiry_date"].astype(str).str[:10]
            opt = opt[opt["expiry"] == wk]
            if opt.empty:
                raise RuntimeError("No CE contracts found for weekly expiry")
            ref = 0.0
            if isinstance(ltp, dict):
                ref = float(next(iter(ltp.values()), 0) or 0)
            opt["dist"] = (opt["strike_price"].astype(float) - ref).abs()
            row = opt.sort_values("dist").iloc[0]
            greeks = client.get_greeks(exchange=inst.exchange, underlying=inst.underlying,
                                       trading_symbol=str(row["trading_symbol"]), expiry=wk)
            return {"contract": row.to_dict(), "greeks": greeks}

        step("5c_greeks_atm_ce", one_option)

    # -- feed --------------------------------------------------------------------------
    def feed_importable() -> dict[str, Any]:
        try:
            from growwapi import GrowwFeed  # noqa: F401
            return {"GrowwFeed_importable": True}
        except Exception as exc:
            return {"GrowwFeed_importable": False, "error": str(exc)}

    step("6_feed_import", feed_importable)
    return finish(0)


def finish(code: int) -> int:
    settings = Settings()
    out = settings.data_dir / "phase0_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(REPORT, default=str, indent=2), encoding="utf-8")
    failed = [k for k, v in REPORT.items() if isinstance(v, dict) and v.get("ok") is False]
    print(f"\nReport written to {out}")
    print("Failed steps:", failed or "none")
    return code if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
