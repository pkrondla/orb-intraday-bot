"""Delta conversion and lot sizing (spec sections 6.2 and 6.3).

The bot always buys the option (a CE for a long signal, a PE for a short one), so the option price
rises when the trade works, for both directions:

    option_SL = entry_LTP - futures_stop_points * |delta|   (sizing / display; the live stop
                                                             is the futures level. The broker
                                                             CE/PE order is a crash net, see
                                                             safety_net_option_stop)
    option_T1 = entry_LTP + futures_T1_points   * |delta|   (display; T1 fires on futures)
    lots      = floor(risk amount / (sizing option points * lot size))

The lots are sized on `stop_points` (`sizing.risk_basis: stop_plan`). For ORB / pivot-breakout
that is the 35-point hard stop (close-through of the level is an extra early exit). Touch
stops still floor at 10 and cap at 35. `hard_stop` sizes on the real stop instead.

Delta and the three prices are fixed at entry. If delta is missing, the stop falls back to the
fixed 20 points of the notes. The notes give no fallback for the targets, so they use the target
delta (0.55) and the plan is flagged. [Assumption]
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from orb_intraday_bot.config.settings import StrategyConfig
from orb_intraday_bot.core.errors import TradePlanError
from orb_intraday_bot.core.models import Direction
from orb_intraday_bot.options.chain import OptionContract, OptionQuote
from orb_intraday_bot.options.selector import FLAG_DELTA_MISSING, OptionSelection
from orb_intraday_bot.risk.sizing import lots_for, risk_budget_inr
from orb_intraday_bot.risk.stops import StopPlan
from orb_intraday_bot.risk.targets import TargetPlan

FLAG_TARGET_DELTA_ASSUMED = "target_delta_assumed"


class OptionPlanError(TradePlanError, ValueError):
    """The option trade cannot be planned (reason in the message)."""


@dataclass(frozen=True, slots=True)
class OptionPlan:
    contract: OptionContract
    direction: Direction
    expiry: date
    futures_entry: float
    strike_label: str
    atm_strike: int
    delta: float | None             # |delta| read from the chain, None if missing
    delta_used: float               # what the target conversion used
    entry_ltp: float
    sl: float
    t1: float
    t2: float
    sl_points: float                # option points from entry to the trade stop
    lots: int
    quantity: int
    t1_book_lots: int               # lots closed at T1 (all but one runner when T2 exists)
    risk_budget_inr: float
    risk_at_stop_inr: float         # loss if the trade is stopped out (the real stop)
    sized_risk_inr: float           # the loss the lot count was sized for (level distance)
    flags: tuple[str, ...]

    @property
    def lot_size(self) -> int:
        return self.contract.lot_size

    @property
    def outlay_inr(self) -> float:
        return self.entry_ltp * self.quantity


def round_to_tick(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 4)


def safety_net_option_stop(
    *,
    planned_sl: float,
    fill: float,
    delta_used: float,
    hard_stop_points: float,
    tick: float,
) -> float:
    """Broker CE/PE trigger: a crash net, never tighter than the plan after a worse fill.

    The live strategy stop is the futures level. Groww only holds this option price so a
    dead bot is not naked. It is the lower of the unshifted plan SL and a 35-point
    conversion from the fill. A worse fill therefore adds rupee risk; it does not pull
    the stop closer to the market.
    """
    from_fill = round_to_tick(fill - hard_stop_points * abs(delta_used), tick)
    return max(tick, round_to_tick(min(planned_sl, from_fill), tick))


def book_lots_at_t1(lots: int, fraction: float | None = None) -> int:
    """Lots closed at T1. One lot: all of them. Two or more: one runner left for T2.

    `fraction` is ignored (retired 50% book). Kept so existing callers still type-check.
    """
    _ = fraction
    if lots <= 1:
        return lots
    return lots - 1


def plan_option_trade(
    *,
    direction: Direction,
    futures_entry: float,
    stop: StopPlan,
    targets: TargetPlan,
    selection: OptionSelection,
    contract: OptionContract,
    config: StrategyConfig,
) -> OptionPlan:
    quote: OptionQuote = selection.quote
    if quote.ltp is None or quote.ltp <= 0:
        raise OptionPlanError(f"{quote.trading_symbol} has no usable price")
    entry = round_to_tick(quote.ltp, contract.tick_size)
    flags = list(selection.flags)

    delta = quote.delta
    # The trade's stop in futures points: 35 for close-based setups, or the touch level.
    stop_futures_points = abs(futures_entry - stop.price_stop)
    if delta is None:
        if FLAG_DELTA_MISSING not in flags:
            flags.append(FLAG_DELTA_MISSING)
        delta_used = config.options.delta_target
        flags.append(FLAG_TARGET_DELTA_ASSUMED)
        stop_option_points = config.stops.option_fallback_points
        sizing_option_points = config.stops.option_fallback_points
    else:
        delta_used = delta
        stop_option_points = stop_futures_points * delta
        sizing_option_points = stop.stop_points * delta    # the level distance (spec section 5)

    sl = round_to_tick(entry - stop_option_points, contract.tick_size)
    if sl <= 0:
        raise OptionPlanError(
            f"Option stop would be {sl:.2f}: premium {entry:.2f} is below the "
            f"{stop_option_points:.1f}-point stop"
        )
    t1 = round_to_tick(entry + targets.t1_points * delta_used, contract.tick_size)
    t2 = round_to_tick(entry + targets.t2_points * delta_used, contract.tick_size)
    sl_points = round(entry - sl, 4)
    single_target = abs(targets.t1 - targets.t2) < 1e-9

    budget = risk_budget_inr(config.sizing)
    if config.sizing.risk_basis == "hard_stop":
        sizing_points = sl_points
    else:
        sized_sl = round_to_tick(entry - sizing_option_points, contract.tick_size)
        sizing_points = round(entry - sized_sl, 4)
    lots = lots_for(budget, sizing_points, contract.lot_size)
    if lots < 1:
        one_lot = sizing_points * contract.lot_size
        raise OptionPlanError(
            f"Risk budget Rs {budget:,.0f} is below one lot's risk Rs {one_lot:,.0f} "
            f"({sizing_points:.1f} option points x {contract.lot_size})"
        )
    quantity = lots * contract.lot_size
    if entry * quantity > config.sizing.capital_inr:
        raise OptionPlanError(
            f"Premium outlay Rs {entry * quantity:,.0f} exceeds capital "
            f"Rs {config.sizing.capital_inr:,.0f}"
        )
    return OptionPlan(
        contract=contract,
        direction=direction,
        expiry=contract.expiry,
        futures_entry=futures_entry,
        strike_label=selection.candidate_label,
        atm_strike=selection.atm_strike,
        delta=delta,
        delta_used=delta_used,
        entry_ltp=entry,
        sl=sl,
        t1=t1,
        t2=t2,
        sl_points=sl_points,
        lots=lots,
        quantity=quantity,
        t1_book_lots=lots if single_target else book_lots_at_t1(lots),
        risk_budget_inr=budget,
        risk_at_stop_inr=round(sl_points * quantity, 2),
        sized_risk_inr=round(sizing_points * quantity, 2),
        flags=tuple(flags),
    )
