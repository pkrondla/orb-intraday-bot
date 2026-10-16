"""Synthetic option data for replays of past days.

There is no historical option chain, so a replay prices every CE/PE near the money at Rs 150
with delta 0.55, and exits are modelled from the futures move. Futures points are the meaningful
result of a replay; the rupee figures only show the mechanics and the size of charges.
"""

from __future__ import annotations

from datetime import date, timedelta

from orb_intraday_bot.options.chain import (
    OptionChain,
    OptionContract,
    OptionKind,
    OptionQuote,
)

PREMIUM = 150.0
DELTA = 0.55
_STRIKES = range(15000, 30001, 50)


class SyntheticOptions:
    def __init__(self, lot_size: int) -> None:
        self._lot = lot_size
        self._chains: dict[date, OptionChain] = {}
        self._contracts: dict[date, dict[str, OptionContract]] = {}

    def nearest_option_expiry(self, today: date) -> date:
        return today + timedelta(days=4)

    def option_chain(self, expiry: date) -> OptionChain:
        if expiry not in self._chains:
            self._chains[expiry] = self._build_chain(expiry)
        return self._chains[expiry]

    def _build_chain(self, expiry: date) -> OptionChain:
        quotes = {
            (strike, kind): OptionQuote(
                strike, kind, f"SYN{strike}{kind.value}", PREMIUM, DELTA
            )
            for strike in _STRIKES
            for kind in OptionKind
        }
        return OptionChain(expiry, None, quotes)

    def option_contracts(self, expiry: date) -> dict[str, OptionContract]:
        if expiry not in self._contracts:
            self._contracts[expiry] = self._build_contracts(expiry)
        return self._contracts[expiry]

    def _build_contracts(self, expiry: date) -> dict[str, OptionContract]:
        return {
            f"SYN{s}{k.value}": OptionContract(
                f"SYN{s}{k.value}", "synthetic", expiry, s, k, self._lot, 0.05
            )
            for s in _STRIKES
            for k in OptionKind
        }
