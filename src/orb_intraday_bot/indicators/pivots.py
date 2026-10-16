"""Traditional daily pivot points (TradingView "Pivot Points Standard", Traditional).

    P  = (H + L + C) / 3
    R1 = 2P - L          S1 = 2P - H
    R2 = P + (H - L)     S2 = P - (H - L)
    R3 = 2P + H - 2L     S3 = 2P + L - 2H
    R4 = 3P + H - 3L     S4 = 3P + L - 3H
    R5 = 4P + H - 4L     S5 = 4P + L - 4H
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from orb_intraday_bot.core.models import Candle


@dataclass(frozen=True, slots=True)
class Pivots:
    p: float
    r1: float
    r2: float
    r3: float
    r4: float
    r5: float
    s1: float
    s2: float
    s3: float
    s4: float
    s5: float

    def levels(self) -> dict[str, float]:
        return {
            "S5": self.s5, "S4": self.s4, "S3": self.s3, "S2": self.s2, "S1": self.s1,
            "P": self.p,
            "R1": self.r1, "R2": self.r2, "R3": self.r3, "R4": self.r4, "R5": self.r5,
        }

    def ordered(self) -> list[tuple[str, float]]:
        return sorted(self.levels().items(), key=lambda item: item[1])

    def levels_between(self, low: float, high: float) -> list[tuple[str, float]]:
        """Pivot levels strictly between two prices, ascending."""
        lo, hi = min(low, high), max(low, high)
        return [(n, v) for n, v in self.ordered() if lo < v < hi]

    def nearest_above(self, price: float) -> tuple[str, float] | None:
        above = [(n, v) for n, v in self.ordered() if v > price]
        return above[0] if above else None

    def nearest_below(self, price: float) -> tuple[str, float] | None:
        below = [(n, v) for n, v in self.ordered() if v < price]
        return below[-1] if below else None


def traditional_pivots(high: float, low: float, close: float) -> Pivots:
    p = (high + low + close) / 3
    return Pivots(
        p=p,
        r1=2 * p - low,
        s1=2 * p - high,
        r2=p + (high - low),
        s2=p - (high - low),
        r3=2 * p + high - 2 * low,
        s3=2 * p + low - 2 * high,
        r4=3 * p + high - 3 * low,
        s4=3 * p + low - 3 * high,
        r5=4 * p + high - 4 * low,
        s5=4 * p + low - 4 * high,
    )


def session_ohlc(candles: Sequence[Candle]) -> tuple[float, float, float]:
    """(high, low, close) of a session's candles, which must be in time order."""
    if not candles:
        raise ValueError("No candles for the session")
    return (
        max(c.high for c in candles),
        min(c.low for c in candles),
        candles[-1].close,
    )


def pivots_from_session(candles: Sequence[Candle]) -> Pivots:
    high, low, close = session_ohlc(candles)
    return traditional_pivots(high, low, close)
