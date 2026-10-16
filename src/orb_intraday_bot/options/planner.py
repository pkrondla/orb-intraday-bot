"""Turns an accepted futures trade into a paper option trade: expiry, strike, delta pricing, lots.

All broker access goes through the small `OptionDataSource` protocol, so the planner is tested
with a fake source and the same code can sit on top of a DhanHQ adapter later. It only reads
data; it never places an order.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from orb_intraday_bot.config.settings import StrategyConfig
from orb_intraday_bot.engine.signal_engine import PlannedTrade
from orb_intraday_bot.options.chain import OptionChain, OptionContract
from orb_intraday_bot.options.pricing import OptionPlan, OptionPlanError, plan_option_trade
from orb_intraday_bot.options.selector import OptionSelectionError, select_option


class OptionDataSource(Protocol):
    def nearest_option_expiry(self, today: date) -> date: ...

    def option_chain(self, expiry: date) -> OptionChain: ...

    def option_contracts(self, expiry: date) -> dict[str, OptionContract]: ...


class OptionPlanner:
    def __init__(self, config: StrategyConfig, source: OptionDataSource) -> None:
        self._cfg = config
        self._source = source
        self._expiry_by_day: dict[date, date] = {}

    def expiry_for(self, today: date) -> date:
        if today not in self._expiry_by_day:
            self._expiry_by_day[today] = self._source.nearest_option_expiry(today)
        return self._expiry_by_day[today]

    def plan(self, trade: PlannedTrade, today: date) -> OptionPlan:
        """Raises `OptionPlanError` (or `OptionSelectionError`) with the reason it cannot trade."""
        signal = trade.signal
        expiry = self.expiry_for(today)
        chain = self._source.option_chain(expiry)
        selection = select_option(
            signal.direction, signal.entry_price, chain, self._cfg.options
        )
        contract = self._source.option_contracts(expiry).get(selection.quote.trading_symbol)
        if contract is None:
            raise OptionPlanError(
                f"{selection.quote.trading_symbol} is not in the instruments file "
                f"(lot size unknown)"
            )
        return plan_option_trade(
            direction=signal.direction,
            futures_entry=signal.entry_price,
            stop=trade.stop,
            targets=trade.targets,
            selection=selection,
            contract=contract,
            config=self._cfg,
        )


__all__ = ["OptionDataSource", "OptionPlanError", "OptionPlanner", "OptionSelectionError"]
