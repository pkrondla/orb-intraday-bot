"""Golden replay: the whole strategy, end to end, frozen as a file.

`replay_snapshot` runs every session in the recorded candle file through the real replay code
(`run_range`: detectors, signal engine, option planner, paper engine, store) and reduces the
outcome to plain JSON: every signal with its verdict and every position with every fill.

A refactor must reproduce `expected_replay.json` exactly. A deliberate behaviour change (a rule,
a stop, a target, a charge) must regenerate it in the same pull request, with a version bump, so
the diff shows which trades moved and why. Regenerate with:

    python -m tests.golden.snapshot --update

Refresh the candle fixture from the local history cache (rarely; it changes the golden):

    python -m tests.golden.snapshot --import-history data/history/NIFTY26OCTFUT.json
"""

from __future__ import annotations

import argparse
import gzip
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from orb_intraday_bot.analysis.backtest import DayResult, run_range
from orb_intraday_bot.analysis.history import load_cache
from orb_intraday_bot.config.settings import StrategyConfig, load_strategy_config
from orb_intraday_bot.core.models import Candle
from orb_intraday_bot.paper.models import Fill, Position

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
CANDLES = HERE / "nifty_fut_3m.json.gz"
EXPECTED = HERE / "expected_replay.json"
DEFAULT_CONFIG = ROOT / "config" / "default.yaml"

SYMBOL = "NIFTY-FUT-GOLDEN"
LOT_SIZE = 65
DECIMALS = 4


def load_candles(path: Path = CANDLES) -> list[Candle]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        raw = json.load(fh)
    from orb_intraday_bot.analysis.history import _decode

    return [_decode(c) for c in raw["candles"]]


def default_config() -> StrategyConfig:
    return load_strategy_config(DEFAULT_CONFIG)


def _t(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _fill(fill: Fill) -> dict[str, Any]:
    return {
        "time": _t(fill.time),
        "reason": str(fill.reason),
        "lots": fill.lots,
        "quantity": fill.quantity,
        "futures_price": round(fill.futures_price, DECIMALS),
        "option_price": round(fill.option_price, DECIMALS),
        "price_source": fill.price_source,
        "option_gross": round(fill.option_gross, DECIMALS),
        "futures_points": round(fill.futures_points, DECIMALS),
        "charges": round(fill.charges, DECIMALS),
    }


def _position(p: Position) -> dict[str, Any]:
    return {
        "id": p.position_id,
        "setup": p.setup,
        "direction": str(p.direction),
        "level": p.level_name,
        "zone": p.zone,
        "opened_at": _t(p.opened_at),
        "closed_at": _t(p.closed_at),
        "status": str(p.status),
        "kind": p.kind,
        "strike": p.strike,
        "lots": p.lots,
        "futures_entry": round(p.futures_entry, DECIMALS),
        "hard_stop": round(p.hard_stop, DECIMALS),
        "entry_ltp": round(p.entry_ltp, DECIMALS),
        "flags": list(p.flags),
        "fills": [_fill(f) for f in p.fills],
    }


def _day(result: DayResult) -> dict[str, Any]:
    return {
        "candles": result.candle_count,
        "signals": [
            [s.signal_id, s.time, s.setup, s.level, s.direction, s.status, s.stage, s.reason]
            for s in result.signals
        ],
        "positions": [_position(p) for p in result.positions],
    }


def replay_snapshot(
    candles: list[Candle] | None = None, cfg: StrategyConfig | None = None
) -> dict[str, Any]:
    results = run_range(
        cfg or default_config(),
        candles if candles is not None else load_candles(),
        SYMBOL,
        LOT_SIZE,
    )
    days = {str(r.day): _day(r) for r in results}
    positions = [p for r in results for p in r.positions]
    return {
        "symbol": SYMBOL,
        "lot_size": LOT_SIZE,
        "summary": {
            "days": len(days),
            "signals": sum(len(d["signals"]) for d in days.values()),
            "trades": len(positions),
            # Unweighted sum over fills (a checksum, not a performance figure).
            "fill_points_checksum": round(
                sum(f.futures_points for p in positions for f in p.fills), DECIMALS
            ),
            "charges": round(sum(f.charges for p in positions for f in p.fills), DECIMALS),
        },
        "days": days,
    }


def dumps(snapshot: dict[str, Any]) -> str:
    """One line per day, so a changed day shows up as a changed line in review."""
    head = {k: v for k, v in snapshot.items() if k != "days"}
    lines = [json.dumps(head, sort_keys=True)[:-1] + ', "days": {']
    items = list(snapshot["days"].items())
    for index, (day, body) in enumerate(items):
        comma = "," if index < len(items) - 1 else ""
        lines.append(f"{json.dumps(day)}: {json.dumps(body, sort_keys=True)}{comma}")
    lines.append("}}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--update", action="store_true", help="rewrite expected_replay.json")
    parser.add_argument("--import-history", type=Path, help="rebuild the candle fixture")
    args = parser.parse_args()
    if args.import_history:
        history = load_cache(args.import_history)
        from orb_intraday_bot.analysis.history import _encode

        payload = json.dumps({"candles": [_encode(c) for c in history.candles]})
        with gzip.GzipFile(CANDLES, "wb", mtime=0) as fh:
            fh.write(payload.encode("utf-8"))
        print(f"wrote {CANDLES} ({len(history.candles)} candles)")
    if args.update:
        snapshot = replay_snapshot()
        EXPECTED.write_text(dumps(snapshot), encoding="utf-8", newline="\n")
        print(f"wrote {EXPECTED}: {json.dumps(snapshot['summary'])}")
    if not (args.update or args.import_history):
        parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
