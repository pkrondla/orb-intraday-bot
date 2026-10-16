"""Phase 6 real-data check: plan a hypothetical long and short option trade at the live price.

Usage:  python scripts/phase6_check_options.py
Read-only. Reads the live futures price, the nearest weekly option chain and the instruments file,
and prints what the bot would pick. The futures stop and targets are fixed example distances
(stop 25, T1 40, T2 120 points); no signal is involved and nothing is recorded or ordered.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orb_intraday_bot.adapters.groww.factory import build_readonly_client  # noqa: E402
from orb_intraday_bot.adapters.groww.market_data import GrowwMarketData  # noqa: E402
from orb_intraday_bot.config import Settings  # noqa: E402
from orb_intraday_bot.config.settings import load_strategy_config  # noqa: E402
from orb_intraday_bot.core.clock import IST  # noqa: E402
from orb_intraday_bot.core.models import Direction  # noqa: E402
from orb_intraday_bot.options.pricing import OptionPlanError, plan_option_trade  # noqa: E402
from orb_intraday_bot.options.selector import (  # noqa: E402
    OptionSelectionError,
    kind_for,
    select_option,
)
from orb_intraday_bot.risk.stops import StopPlan  # noqa: E402
from orb_intraday_bot.risk.targets import TargetPlan  # noqa: E402
from orb_intraday_bot.runtime.guard import run_startup_checks  # noqa: E402

STOP, T1, T2 = 25.0, 40.0, 120.0


def main() -> int:
    settings = Settings()
    run_startup_checks(settings)
    cfg = load_strategy_config(ROOT / settings.strategy_config_path)
    md = GrowwMarketData(build_readonly_client(settings), cfg.instrument)

    today = datetime.now(IST).date()
    fut = md.futures_contract(today)
    price = md.ltp([fut.trading_symbol])[fut.trading_symbol]
    expiry = md.nearest_option_expiry(today)
    chain = md.option_chain(expiry)
    contracts = md.option_contracts(expiry)
    print(f"{fut.trading_symbol} LTP {price}   spot {chain.underlying_ltp}   "
          f"weekly expiry {expiry}   chain quotes {len(chain.quotes)}")

    for direction in (Direction.LONG, Direction.SHORT):
        sign = 1 if direction is Direction.LONG else -1
        print(f"\n--- {direction.value} -> buy {kind_for(direction).value} ---")
        try:
            sel = select_option(direction, price, chain, cfg.options)
        except OptionSelectionError as exc:
            print(f"  cannot select: {exc}")
            continue
        for label, q in sel.considered:
            mark = "  <== chosen" if q is sel.quote else ""
            print(f"  {label:<5} {q.trading_symbol}  delta {q.delta}  ltp {q.ltp}{mark}")
        contract = contracts.get(sel.quote.trading_symbol)
        if contract is None:
            print("  not in the instruments file")
            continue
        try:
            plan = plan_option_trade(
                direction=direction,
                futures_entry=price,
                stop=StopPlan(STOP, price - sign * 35, None, "example"),
                targets=TargetPlan(price + sign * T1, price + sign * T2, T1, T2, "ex", "ex"),
                selection=sel,
                contract=contract,
                config=cfg,
            )
        except OptionPlanError as exc:
            print(f"  cannot plan: {exc}")
            continue
        print(f"  entry {plan.entry_ltp}  SL {plan.sl} ({plan.sl_points} pts)  "
              f"T1 {plan.t1}  T2 {plan.t2}  delta {plan.delta}")
        print(f"  lot size {plan.lot_size}  lots {plan.lots}  qty {plan.quantity}  "
              f"book {plan.t1_book_lots} lot(s) at T1")
        print(f"  sized for Rs {plan.sized_risk_inr:,.0f} (budget "
              f"Rs {plan.risk_budget_inr:,.0f}), loss at the stop "
              f"Rs {plan.risk_at_stop_inr:,.0f}  outlay Rs {plan.outlay_inr:,.0f}  "
              f"flags {list(plan.flags) or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
