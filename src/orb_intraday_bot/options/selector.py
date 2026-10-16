"""Strike selection (spec section 6.1).

The strike comes from the Nifty futures price: round to the nearest strike step to get ATM, then
consider ATM and the one-strike-in-the-money strike (lower for a CE, higher for a PE). Of those,
take the one whose |delta| is inside the band (0.50 to 0.60), closest to 0.55. If none is in the
band, take the closest to 0.55 and flag it. If the chain has no delta at all, take ATM and flag it
(the option stop then uses the fixed fallback of section 6.2).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from orb_intraday_bot.config.settings import OptionsConfig
from orb_intraday_bot.core.models import Direction
from orb_intraday_bot.options.chain import OptionChain, OptionKind, OptionQuote

FLAG_DELTA_OUT_OF_BAND = "delta_out_of_band"
FLAG_DELTA_MISSING = "delta_missing"


class OptionSelectionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OptionSelection:
    quote: OptionQuote
    atm_strike: int
    candidate_label: str                      # "atm" or "itm1"
    flags: tuple[str, ...]
    considered: tuple[tuple[str, OptionQuote], ...]


def kind_for(direction: Direction) -> OptionKind:
    return OptionKind.CE if direction is Direction.LONG else OptionKind.PE


def atm_strike(futures_price: float, step: int) -> int:
    """Nearest strike, with halves rounding up."""
    return int(math.floor(futures_price / step + 0.5)) * step


def candidate_strikes(
    direction: Direction, futures_price: float, cfg: OptionsConfig
) -> list[tuple[str, int]]:
    atm = atm_strike(futures_price, cfg.strike_step)
    # In the money: below spot for a call, above spot for a put.
    itm1 = atm - cfg.strike_step if direction is Direction.LONG else atm + cfg.strike_step
    mapping = {"atm": atm, "itm1": itm1}
    return [(name, mapping[name]) for name in cfg.candidates]


def select_option(
    direction: Direction,
    futures_price: float,
    chain: OptionChain,
    cfg: OptionsConfig,
) -> OptionSelection:
    kind = kind_for(direction)
    atm = atm_strike(futures_price, cfg.strike_step)
    found: list[tuple[str, OptionQuote]] = []
    for label, strike in candidate_strikes(direction, futures_price, cfg):
        quote = chain.get(strike, kind)
        if quote is not None and quote.ltp is not None and quote.trading_symbol:
            found.append((label, quote))
    if not found:
        raise OptionSelectionError(
            f"No priced {kind.value} at the candidate strikes around {atm} "
            f"for expiry {chain.expiry}"
        )

    def distance(item: tuple[str, OptionQuote]) -> float:
        delta = item[1].delta
        assert delta is not None
        return abs(delta - cfg.delta_target)

    with_delta = [item for item in found if item[1].delta is not None]
    in_band = [
        item
        for item in with_delta
        # Rounded so 0.5 and 0.6 count as inside the band despite float noise.
        if cfg.delta_min <= round(item[1].delta or 0.0, 4) <= cfg.delta_max
    ]
    flags: tuple[str, ...]
    if in_band:
        chosen = min(in_band, key=distance)    # min() keeps the first (ATM) on a tie
        flags = ()
    elif with_delta:
        chosen = min(with_delta, key=distance)
        flags = (FLAG_DELTA_OUT_OF_BAND,)
    else:
        chosen = found[0]
        flags = (FLAG_DELTA_MISSING,)
    return OptionSelection(
        quote=chosen[1],
        atm_strike=atm,
        candidate_label=chosen[0],
        flags=flags,
        considered=tuple(found),
    )
