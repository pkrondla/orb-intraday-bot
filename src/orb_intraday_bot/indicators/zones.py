"""Market zone from the pivot levels: above R1 is bullish, below S1 is bearish, else neutral."""

from __future__ import annotations

from orb_intraday_bot.core.models import Zone
from orb_intraday_bot.indicators.pivots import Pivots


def classify_zone(price: float, pivots: Pivots) -> Zone:
    if price > pivots.r1:
        return Zone.BULLISH
    if price < pivots.s1:
        return Zone.BEARISH
    return Zone.NEUTRAL


def band_label(price: float, pivots: Pivots) -> str:
    """A readable position such as 'R1-R2' or 'P-S1', for the per-candle report."""
    above = pivots.nearest_above(price)
    below = pivots.nearest_below(price)
    if above is None and below is not None:
        return f"above {below[0]}"
    if below is None and above is not None:
        return f"below {above[0]}"
    if above is None or below is None:
        return "n/a"
    return f"{below[0]}-{above[0]}"
