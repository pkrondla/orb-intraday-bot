from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import pytest

from orb_intraday_bot.adapters.groww.market_data import GrowwMarketData
from orb_intraday_bot.config.settings import InstrumentConfig, StrategyConfig
from orb_intraday_bot.core.clock import FixedClock
from orb_intraday_bot.core.models import Direction, Zone
from orb_intraday_bot.core.signals import Setup, SetupSignal
from orb_intraday_bot.engine.signal_engine import PlannedTrade
from orb_intraday_bot.options.chain import (
    OptionChain,
    OptionContract,
    OptionKind,
    parse_option_chain,
    parse_option_contracts,
)
from orb_intraday_bot.options.planner import OptionPlanner
from orb_intraday_bot.options.pricing import (
    FLAG_TARGET_DELTA_ASSUMED,
    OptionPlanError,
    book_lots_at_t1,
    plan_option_trade,
    safety_net_option_stop,
)
from orb_intraday_bot.options.selector import (
    FLAG_DELTA_MISSING,
    FLAG_DELTA_OUT_OF_BAND,
    OptionSelectionError,
    atm_strike,
    candidate_strikes,
    select_option,
)
from orb_intraday_bot.risk.stops import StopPlan
from orb_intraday_bot.risk.targets import TargetPlan
from tests.helpers import at

EXPIRY = date(2026, 10, 19)
CFG = StrategyConfig()


def side(symbol: str, delta: float, ltp: float, oi: float = 1000) -> dict[str, Any]:
    return {"greeks": {"delta": delta, "iv": 13.0}, "trading_symbol": symbol, "ltp": ltp,
            "open_interest": oi, "volume": 0}


# Real quotes from the Phase 0 spike (futures 22998.5, spot 22965.35).
REAL = {
    "underlying_ltp": 22965.35,
    "strikes": {
        "22900": {"CE": side("NIFTY26O1922900CE", 0.579, 171.25),
                  "PE": side("NIFTY26O1922900PE", -0.421, 107.35)},
        "22950": {"CE": side("NIFTY26O1922950CE", 0.5217, 141.0),
                  "PE": side("NIFTY26O1922950PE", -0.4783, 127.35)},
        "23000": {"CE": side("NIFTY26O1923000CE", 0.4615, 114.1),
                  "PE": side("NIFTY26O1923000PE", -0.5385, 150.6)},
        "23050": {"CE": side("NIFTY26O1923050CE", 0.4001, 90.45),
                  "PE": side("NIFTY26O1923050PE", -0.5999, 176.8)},
    },
}


def chain(raw: dict[str, Any] | None = None) -> OptionChain:
    return parse_option_chain(raw or REAL, EXPIRY)


def contract(symbol: str = "NIFTY26O1922950CE", strike: int = 22950,
             kind: OptionKind = OptionKind.CE, lot: int = 65) -> OptionContract:
    return OptionContract(symbol, f"NSE-{symbol}", EXPIRY, strike, kind, lot, 0.05)


# ---- chain parsing -------------------------------------------------------------------------
def test_parse_chain_reads_quotes_with_absolute_delta() -> None:
    c = chain()
    assert c.underlying_ltp == 22965.35
    ce = c.get(22950, OptionKind.CE)
    pe = c.get(22950, OptionKind.PE)
    assert ce is not None and pe is not None
    assert ce.delta == 0.5217 and pe.delta == 0.4783      # PE delta is stored as a magnitude
    assert ce.trading_symbol == "NIFTY26O1922950CE" and ce.ltp == 141.0


def test_zero_wrong_sign_and_bad_delta_are_missing() -> None:
    raw = {"strikes": {"23000": {
        "CE": side("A", -0.5, 100),            # negative call delta is wrong
        "PE": side("B", 0, 100),               # zero means no value
    }, "23050": {
        "CE": side("C", 5.0, 100),             # not a delta
        "PE": side("D", 0.5, 100),             # positive put delta is wrong
    }}}
    c = parse_option_chain(raw, EXPIRY)
    assert [c.get(s, k).delta for s, k in  # type: ignore[union-attr]
            [(23000, OptionKind.CE), (23000, OptionKind.PE),
             (23050, OptionKind.CE), (23050, OptionKind.PE)]] == [None] * 4


def test_zero_or_missing_price_is_unusable() -> None:
    raw = {"strikes": {"23000": {"CE": side("A", 0.5, 0), "PE": {"trading_symbol": "B"}}}}
    c = parse_option_chain(raw, EXPIRY)
    assert c.get(23000, OptionKind.CE).ltp is None  # type: ignore[union-attr]
    assert c.get(23000, OptionKind.PE).ltp is None  # type: ignore[union-attr]


def test_parse_chain_tolerates_empty_response() -> None:
    assert parse_option_chain({}, EXPIRY).quotes == {}


def test_parse_option_contracts_filters_expiry_kind_and_underlying() -> None:
    rows = [
        {"underlying_symbol": "NIFTY", "instrument_type": "CE", "expiry_date": "2026-10-19",
         "trading_symbol": "NIFTY26O1923000CE", "groww_symbol": "g", "strike_price": "23000",
         "lot_size": "65", "tick_size": "0.05"},
        {"underlying_symbol": "NIFTY", "instrument_type": "CE", "expiry_date": "2026-10-27",
         "trading_symbol": "NIFTY26OCT23000CE", "groww_symbol": "g", "strike_price": "23000",
         "lot_size": "65", "tick_size": "0.05"},
        {"underlying_symbol": "NIFTY", "instrument_type": "FUT", "expiry_date": "2026-10-19",
         "trading_symbol": "F", "groww_symbol": "g", "strike_price": "0",
         "lot_size": "65", "tick_size": "0.1"},
        {"underlying_symbol": "BANKNIFTY", "instrument_type": "PE", "expiry_date": "2026-10-19",
         "trading_symbol": "BN", "groww_symbol": "g", "strike_price": "50000",
         "lot_size": "30", "tick_size": "0.05"},
    ]
    found = parse_option_contracts(rows, EXPIRY)
    assert list(found) == ["NIFTY26O1923000CE"]
    only = found["NIFTY26O1923000CE"]
    assert only.lot_size == 65 and only.strike == 23000 and only.kind is OptionKind.CE


# ---- strike selection ----------------------------------------------------------------------
def test_atm_rounds_to_nearest_step_with_halves_up() -> None:
    assert atm_strike(22998.5, 50) == 23000
    assert atm_strike(22974.9, 50) == 22950
    assert atm_strike(22975.0, 50) == 23000


def test_itm_is_lower_for_calls_and_higher_for_puts() -> None:
    assert candidate_strikes(Direction.LONG, 23000, CFG.options) == [
        ("atm", 23000), ("itm1", 22950)]
    assert candidate_strikes(Direction.SHORT, 23000, CFG.options) == [
        ("atm", 23000), ("itm1", 23050)]


def test_real_example_long_picks_itm_call_because_atm_delta_is_too_low() -> None:
    # 23000 CE delta 0.4615 is out of the band, 22950 CE delta 0.5217 is inside.
    sel = select_option(Direction.LONG, 22998.5, chain(), CFG.options)
    assert (sel.quote.strike, sel.candidate_label, sel.flags) == (22950, "itm1", ())
    assert sel.atm_strike == 23000


def test_real_example_short_picks_atm_put_closest_to_target() -> None:
    # Both 23000 PE (0.5385) and 23050 PE (0.5999) are in the band; 0.5385 is closer to 0.55.
    sel = select_option(Direction.SHORT, 22998.5, chain(), CFG.options)
    assert (sel.quote.strike, sel.candidate_label, sel.flags) == (23000, "atm", ())
    assert sel.quote.trading_symbol == "NIFTY26O1923000PE"


def test_band_edges_are_inclusive() -> None:
    for edge, outside in ((0.5, 0.4999), (0.6, 0.6001)):
        raw = {"strikes": {
            "23000": {"CE": side("A", outside, 100)},
            "22950": {"CE": side("B", edge, 120)},
        }}
        sel = select_option(Direction.LONG, 23000, parse_option_chain(raw, EXPIRY), CFG.options)
        assert sel.quote.trading_symbol == "B" and sel.flags == ()


def test_nothing_in_band_takes_closest_and_flags() -> None:
    raw = {"strikes": {
        "23000": {"CE": side("A", 0.42, 100)},
        "22950": {"CE": side("B", 0.47, 120)},
    }}
    sel = select_option(Direction.LONG, 23000, parse_option_chain(raw, EXPIRY), CFG.options)
    assert sel.quote.trading_symbol == "B"
    assert sel.flags == (FLAG_DELTA_OUT_OF_BAND,)


def test_no_delta_anywhere_takes_atm_and_flags() -> None:
    raw = {"strikes": {
        "23000": {"CE": side("A", 0, 100)},
        "22950": {"CE": side("B", 0, 120)},
    }}
    sel = select_option(Direction.LONG, 23000, parse_option_chain(raw, EXPIRY), CFG.options)
    assert sel.quote.trading_symbol == "A"
    assert sel.flags == (FLAG_DELTA_MISSING,)


def test_candidate_without_price_is_skipped() -> None:
    raw = {"strikes": {
        "23000": {"CE": side("A", 0.55, 0)},          # in band but no price
        "22950": {"CE": side("B", 0.47, 120)},
    }}
    sel = select_option(Direction.LONG, 23000, parse_option_chain(raw, EXPIRY), CFG.options)
    assert sel.quote.trading_symbol == "B"


def test_no_priced_candidates_raises() -> None:
    with pytest.raises(OptionSelectionError):
        select_option(Direction.LONG, 25000, chain(), CFG.options)


def test_atm_only_candidate_list() -> None:
    cfg = StrategyConfig.model_validate({"options": {"candidates": ["atm"]}})
    sel = select_option(Direction.LONG, 22998.5, chain(), cfg.options)
    assert sel.quote.strike == 23000 and sel.flags == (FLAG_DELTA_OUT_OF_BAND,)


# ---- delta conversion and lots -------------------------------------------------------------
def plan_for(
    *,
    ltp: float = 150.0,
    delta: float | None = 0.55,
    stop_points: float = 40.0,
    t1_points: float = 60.0,
    t2_points: float = 120.0,
    config: StrategyConfig = CFG,
    lot: int = 65,
    direction: Direction = Direction.LONG,
) -> Any:
    raw = {"strikes": {"22600": {"CE": side("X", delta or 0, ltp)}}}
    sel = select_option(Direction.LONG, 22600, parse_option_chain(raw, EXPIRY), config.options)
    return plan_option_trade(
        direction=direction,
        futures_entry=22600.0,
        stop=StopPlan(stop_points, 22600 - stop_points, None, "test"),
        targets=TargetPlan(22600 + t1_points, 22600 + t2_points, t1_points, t2_points, "a", "b"),
        selection=sel,
        contract=contract("X", 22600, lot=lot),
        config=config,
    )


def test_worked_example_from_the_spec() -> None:
    plan = plan_for()
    assert (plan.entry_ltp, plan.sl, plan.t1, plan.t2) == (150.0, 128.0, 183.0, 216.0)
    assert plan.sl_points == 22.0
    assert plan.delta == 0.55 and plan.flags == ()


def test_lots_follow_the_risk_budget() -> None:
    plan = plan_for()
    # Budget 5,000 / (22 pts x 65) = 3.49 -> 3 lots.
    assert plan.lots == 3 and plan.quantity == 195
    assert plan.risk_budget_inr == 5000
    assert plan.risk_at_stop_inr == 22 * 195
    assert plan.risk_at_stop_inr <= plan.risk_budget_inr
    assert plan.t1_book_lots == 2                          # 3 lots: one runner for T2


def close_stop_plan(config: StrategyConfig = CFG, delta: float | None = 0.55) -> Any:
    """A close-based setup: 35-point hard stop plus close-through of the level."""
    selection = select_option(
        Direction.LONG, 22600,
        parse_option_chain({"strikes": {"22600": {"CE": side("X", delta or 0, 150.0)}}}, EXPIRY),
        config.options,
    )
    return plan_option_trade(
        direction=Direction.LONG, futures_entry=22600.0,
        stop=StopPlan(35.0, 22565.0, 22584.0, "close through ORH or 35-pt hard stop"),
        targets=TargetPlan(22700.0, 22700.0, 100, 100, "R2", "R2"),
        selection=selection, contract=contract("X", 22600), config=config,
    )


def test_close_based_stop_sizes_on_35() -> None:
    plan = close_stop_plan()
    assert plan.sl == 130.75 and plan.sl_points == 19.25        # 35 x 0.55
    assert plan.lots == 3                                       # 5000 / (19.25 x 65) = 3.99
    assert plan.sized_risk_inr == pytest.approx(19.25 * 3 * 65, abs=1)
    assert plan.risk_at_stop_inr == plan.sized_risk_inr
    assert plan.t1_book_lots == plan.lots


def test_touch_stop_uses_the_same_distance_for_stop_and_lots() -> None:
    plan = plan_for(stop_points=22.0)
    assert plan.sl_points == 12.1                               # 22 futures points x 0.55
    assert plan.sized_risk_inr == plan.risk_at_stop_inr


def test_missing_delta_stop_and_sizing_use_the_fixed_fallback() -> None:
    plan = close_stop_plan(delta=None)
    assert plan.sl == 130.0 and plan.sl_points == 20.0
    assert plan.lots == 3 and plan.sized_risk_inr == plan.risk_at_stop_inr


def test_hard_35_point_stop_gives_about_19_option_points() -> None:
    plan = plan_for(stop_points=35, ltp=150)
    assert plan.sl_points == 19.25 and plan.sl == 130.75


def test_optional_hard_stop_basis_matches_when_stop_points_are_35() -> None:
    default = close_stop_plan()
    safe = close_stop_plan(StrategyConfig.model_validate({"sizing": {"risk_basis": "hard_stop"}}))
    assert default.lots == safe.lots == 3
    assert safe.risk_at_stop_inr <= safe.risk_budget_inr
    assert safe.sl == default.sl


def test_missing_delta_uses_fixed_option_stop_and_flags() -> None:
    plan = plan_for(delta=None)
    assert plan.sl == 130.0 and plan.sl_points == 20.0          # fixed 20-point fallback
    assert plan.delta is None and plan.delta_used == 0.55
    assert plan.t1 == 150 + 60 * 0.55
    assert FLAG_DELTA_MISSING in plan.flags and FLAG_TARGET_DELTA_ASSUMED in plan.flags


def test_prices_are_rounded_to_the_tick() -> None:
    plan = plan_for(ltp=150.03, delta=0.5217, stop_points=33.3, t1_points=41.7, t2_points=77.7)
    for price in (plan.entry_ltp, plan.sl, plan.t1, plan.t2):
        assert round(price / 0.05) * 0.05 == pytest.approx(price)


def test_short_trades_also_buy_an_option_that_rises() -> None:
    plan = plan_for(direction=Direction.SHORT)
    assert plan.sl < plan.entry_ltp < plan.t1 < plan.t2


def test_budget_too_small_for_one_lot_is_rejected() -> None:
    with pytest.raises(OptionPlanError, match="below one lot"):
        plan_for(lot=400)       # 22 pts x 400 = 8,800 > 5,000


def test_premium_below_the_stop_is_rejected() -> None:
    with pytest.raises(OptionPlanError, match="below the"):
        plan_for(ltp=15.0)      # stop of 22 points on a 15 rupee premium


def test_outlay_above_capital_is_rejected() -> None:
    cfg = StrategyConfig.model_validate(
        {"sizing": {"capital_inr": 20_000, "risk_pct_per_trade": 0.5}})
    with pytest.raises(OptionPlanError, match="exceeds capital"):
        plan_for(config=cfg)


def test_book_lots_at_t1() -> None:
    assert [book_lots_at_t1(n) for n in (0, 1, 2, 3, 4, 5, 11)] == [0, 1, 1, 2, 3, 4, 10]


def test_safety_net_does_not_tighten_after_a_worse_fill() -> None:
    # This morning's PE: 35-pt plan, fill 3.55 above the quote. Net stays at the plan.
    assert safety_net_option_stop(
        planned_sl=219.75, fill=241.50, delta_used=0.52, hard_stop_points=35, tick=0.05,
    ) == pytest.approx(219.75)
    # This afternoon's ORB: 28.9-pt plan 215.10, 35-pt from fill is 216.60. Keep 215.10.
    assert safety_net_option_stop(
        planned_sl=215.10, fill=235.50, delta_used=0.54, hard_stop_points=35, tick=0.05,
    ) == pytest.approx(215.10)


def test_safety_net_uses_the_35_point_floor_when_that_is_looser() -> None:
    # 20-pt structure stop from the quote is 139.00; 35 pts from a 150.20 fill is 130.95.
    assert safety_net_option_stop(
        planned_sl=139.00, fill=150.20, delta_used=0.55, hard_stop_points=35, tick=0.05,
    ) == pytest.approx(130.95)


# ---- planner and Groww adapter with fakes --------------------------------------------------
def planned(direction: Direction = Direction.LONG, entry: float = 22998.5) -> PlannedTrade:
    signal = SetupSignal(
        setup=Setup.ORB_BREAKOUT, direction=direction, level_name="ORH", level_value=22960.0,
        candle_start=at(9, 33), entry_price=entry, entry_basis="test", sign_id="s1",
        stop_level=22960.0, stop_on="close", target_ref="R2",
    )
    sign = 1 if direction is Direction.LONG else -1
    return PlannedTrade(
        signal=signal,
        zone=Zone.NEUTRAL,
        stop=StopPlan(20.0, entry - sign * 35, 22960.0, "test"),
        targets=TargetPlan(entry + sign * 30, entry + sign * 120, 30, 120, "t1", "t2"),
        risk_budget_inr=5000,
    )


class FakeSource:
    def __init__(self, contracts: dict[str, OptionContract]) -> None:
        self.contracts = contracts
        self.expiry_calls = 0
        self.chain_calls = 0

    def nearest_option_expiry(self, today: date) -> date:
        self.expiry_calls += 1
        return EXPIRY

    def option_chain(self, expiry: date) -> OptionChain:
        self.chain_calls += 1
        return chain()

    def option_contracts(self, expiry: date) -> dict[str, OptionContract]:
        return self.contracts


def test_planner_builds_a_plan_and_caches_expiry() -> None:
    source = FakeSource({"NIFTY26O1922950CE": contract(), "NIFTY26O1923000PE": contract(
        "NIFTY26O1923000PE", 23000, OptionKind.PE)})
    planner = OptionPlanner(CFG, source)
    long_plan = planner.plan(planned(), date(2026, 10, 15))
    short_plan = planner.plan(planned(Direction.SHORT), date(2026, 10, 15))
    assert long_plan.contract.trading_symbol == "NIFTY26O1922950CE"
    assert long_plan.lot_size == 65 and long_plan.expiry == EXPIRY
    # The 35-point stop, not the 20-point level distance the lots were sized on.
    assert long_plan.sl == pytest.approx(141.0 - 35 * 0.5217, abs=0.03)
    assert short_plan.contract.trading_symbol == "NIFTY26O1923000PE"
    assert source.expiry_calls == 1 and source.chain_calls == 2


def test_planner_refuses_when_lot_size_is_unknown() -> None:
    planner = OptionPlanner(CFG, FakeSource({}))
    with pytest.raises(OptionPlanError, match="instruments file"):
        planner.plan(planned(), date(2026, 10, 15))


class FakeGrowwClient:
    CANDLE_INTERVAL_MIN_3 = "3minute"

    def __init__(self) -> None:
        self.chain_args: dict[str, Any] = {}

    def get_all_instruments(self) -> pd.DataFrame:
        return pd.DataFrame([
            {"underlying_symbol": "NIFTY", "instrument_type": "CE", "expiry_date": "2026-10-19",
             "trading_symbol": "NIFTY26O1922950CE", "groww_symbol": "g", "strike_price": "22950",
             "lot_size": "65", "tick_size": "0.05"},
        ])

    def get_option_chain(self, **kw: Any) -> dict[str, Any]:
        self.chain_args = kw
        return REAL


def test_groww_adapter_fetches_chain_and_contracts() -> None:
    client = FakeGrowwClient()
    md = GrowwMarketData(client, InstrumentConfig(), FixedClock(at(12, 0)))
    c = md.option_chain(EXPIRY)
    assert client.chain_args == {"exchange": "NSE", "underlying": "NIFTY",
                                 "expiry_date": "2026-10-19"}
    assert c.get(22950, OptionKind.CE) is not None
    contracts = md.option_contracts(EXPIRY)
    assert contracts["NIFTY26O1922950CE"].lot_size == 65
    assert md.option_contracts(EXPIRY) is contracts       # cached
